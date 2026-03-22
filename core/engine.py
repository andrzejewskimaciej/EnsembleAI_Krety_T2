import os
import jsonlines
import argparse
import re
import logging
import concurrent.futures

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

FILE_SEP_SYMBOL = "<|file_sep|>"
MAX_CONTEXT_CHARS = 24000 
MAX_CHARS_PER_FILE = 4000

argparser = argparse.ArgumentParser()
argparser.add_argument("--stage", type=str, default="start")
argparser.add_argument("--lang", type=str, default="python")
argparser.add_argument("--strategy", type=str, default="regex-definition")
argparser.add_argument("--trim-prefix", action="store_true")
argparser.add_argument("--trim-suffix", action="store_true")
argparser.add_argument("--limit", type=int, default=None)

try:
    args = argparser.parse_args()
except Exception:
    args = argparse.Namespace(stage='start', lang='python', strategy='regex-definition', trim_prefix=True, trim_suffix=True, limit=None)

def build_repo_cache(repo_path: str) -> dict:
    repo_cache = {}
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, repo_path)
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    definitions = set(re.findall(r'^(?:async\s+)?(?:def|class)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE))
                    repo_cache[rel_path] = {
                        "content": content,
                        "definitions": definitions,
                        "all_words": set(re.findall(r'[a-zA-Z_]\w{3,}', content))
                    }
                except Exception:
                    continue
    return repo_cache

def extract_local_words(text: str) -> set:
    words = re.findall(r'[a-zA-Z_]\w{2,}', text)
    stopwords = {"import", "from", "return", "class", "def", "self", "pass", "True", "False", "None", "print", "with", "open"}
    return set(words) - stopwords

def get_context(datapoint: dict, repo_cache: dict) -> tuple:
    prefix = datapoint.get('prefix') or ""
    suffix = datapoint.get('suffix') or ""
    target_path = datapoint.get('path', '')
    
    last_lines = "\n".join(prefix.split("\n")[-20:])
    first_lines = "\n".join(suffix.split("\n")[:20])
    target_vocab = extract_local_words(last_lines + "\n" + first_lines)
    
    tier_1_defs = []
    tier_2_overlap = []
    
    for rel_path, data in repo_cache.items():
        if rel_path == target_path:
            continue 
            
        def_overlap = target_vocab.intersection(data["definitions"])
        if def_overlap:
            tier_1_defs.append((len(def_overlap), rel_path, data["content"]))
            continue
            
        overlap = len(target_vocab.intersection(data["all_words"]))
        if overlap > 3:
            tier_2_overlap.append((overlap, rel_path, data["content"]))
            
    tier_1_defs.sort(key=lambda x: x[0], reverse=True)
    tier_2_overlap.sort(key=lambda x: x[0], reverse=True)
    
    waterfall = [tier_1_defs, tier_2_overlap]
    accepted_parts = []
    current_chars = 0
    
    for layer in waterfall:
        for score, rel_path, content in layer:
            if len(content) > MAX_CHARS_PER_FILE:
                content = content[:MAX_CHARS_PER_FILE] + "\n# ... [TRUNCATED]"
                
            part = f"File: {rel_path}\n{content}\n{FILE_SEP_SYMBOL}\n"
            
            if current_chars + len(part) > MAX_CONTEXT_CHARS:
                break
            else:
                accepted_parts.append(part)
                current_chars += len(part)
                
    accepted_parts.reverse()
    
    return "".join(accepted_parts), len(accepted_parts)

def process_datapoint(payload: dict):
    datapoint = payload['datapoint']
    repo_cache = payload['repo_cache']
    trim_p = payload['trim_prefix']
    trim_s = payload['trim_suffix']
    
    prefix = datapoint.get('prefix') or ""
    suffix = datapoint.get('suffix') or ""
    datapoint['prefix'] = prefix
    datapoint['suffix'] = suffix
    
    try:
        context_str, files_included = get_context(datapoint, repo_cache)
        
        submission = {"context": context_str}
        if trim_p: submission["prefix"] = "\n".join(prefix.split("\n")[-15:])
        if trim_s: submission["suffix"] = "\n".join(suffix.split("\n")[:15])
        
        return submission, files_included
    except Exception as e:
        logger.error(f"CRASH on {datapoint.get('path', 'unknown')}: {repr(e)}")
        submission = {"context": f"Project Map: Error\n{FILE_SEP_SYMBOL}\n"}
        if trim_p: submission["prefix"] = "\n".join(prefix.split("\n")[-15:])
        if trim_s: submission["suffix"] = "\n".join(suffix.split("\n")[:15])
        return submission, 0

def expand_repo_path(repo_path_hash: str, language: str, stage: str) -> str:
    base = "data"
    variations = [f"repositories-{language}-{stage}", f"{language}-{stage}", f"repositories-{language}-dataset"]
    for var in variations:
        candidate = os.path.join(base, var, repo_path_hash)
        if os.path.isdir(candidate): return candidate
    return os.path.join(base, f"repositories-{language}-{stage}", repo_path_hash)

def main():
    stage, language, strategy, limit = args.stage, args.lang, args.strategy, args.limit
    logger.info(f"Running Regex-Definition Engine on stage '{stage}'")

    completion_points_file = os.path.join("data", f"{language}-{stage}.jsonl")
    predictions_file = os.path.join("predictions", f"{language}-{stage}-{strategy}.jsonl")
    os.makedirs("predictions", exist_ok=True)

    raw_datapoints = []
    with jsonlines.open(completion_points_file, 'r') as reader:
        for i, dp in enumerate(reader):
            if limit and i >= limit: break
            raw_datapoints.append(dp)
            
    total_datapoints = len(raw_datapoints)
    if total_datapoints == 0: return

    logger.info("Caching Repositories & Parsing Regex Definitions...")
    payloads = []
    repo_caches = {}
    
    for dp in raw_datapoints:
        repo_hash = f"{dp['repo'].replace('/', '__')}-{dp['revision']}"
        root_directory = expand_repo_path(repo_hash, language, stage)
        
        if root_directory not in repo_caches:
            repo_caches[root_directory] = build_repo_cache(root_directory)
            
        payloads.append({
            'datapoint': dp, 
            'repo_cache': repo_caches[root_directory],
            'trim_prefix': args.trim_prefix, 
            'trim_suffix': args.trim_suffix
        })
            
    results = []
    total_files_appended = 0
    
    logger.info("Scoring and assembling context...")
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for count, (sub, files_included) in enumerate(executor.map(process_datapoint, payloads), 1):
            if sub: results.append(sub)
            total_files_appended += files_included
            if count % 10 == 0: logger.info(f"Processed {count}/{total_datapoints} datapoints.")
                
    logger.info(f"Writing {len(results)} predictions to {predictions_file}")
    with jsonlines.open(predictions_file, 'w') as writer:
        for res in results: writer.write(res)
            
    print(f"\n{'='*40}\n      EXECUTION LOG REPORT\n{'='*40}")
    print(f"Total Datapoints: {total_datapoints}")
    print(f"Average Files Injected Per Prompt: {total_files_appended / total_datapoints:.2f}")
    print(f"{'='*40}\n")

if __name__ == "__main__":
    main()