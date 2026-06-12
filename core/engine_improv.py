import os
import ast
import zipfile
import jsonlines
import argparse
import re
import math
import logging
import concurrent.futures
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

FILE_SEP_SYMBOL = "<|file_sep|>"
MAX_CONTEXT_CHARS = 24000
MAX_CHARS_PER_FILE = 4000
WINDOW_LINES = 40

argparser = argparse.ArgumentParser()
argparser.add_argument("--stage", type=str, default="start")
argparser.add_argument("--lang", type=str, default="python")
argparser.add_argument("--strategy", type=str, default="improv")
argparser.add_argument("--trim-prefix", action="store_true")
argparser.add_argument("--trim-suffix", action="store_true")
argparser.add_argument("--limit", type=int, default=None)

try:
    args = argparser.parse_args()
except (Exception, SystemExit):
    args = argparse.Namespace(stage='practice', lang='python', strategy='improv',
                              trim_prefix=True, trim_suffix=True, limit=None)


PYTHON_STOPWORDS = {
    "import", "from", "return", "class", "def", "self", "pass", "True",
    "False", "None", "print", "with", "open", "raise", "assert", "yield",
    "lambda", "global", "nonlocal", "async", "await", "else", "elif",
    "while", "break", "continue", "except", "finally", "try", "for",
    "isinstance", "hasattr", "getattr", "setattr", "super", "type",
    "list", "dict", "tuple", "set", "str", "int", "float", "bool",
    "range", "enumerate", "zip", "map", "filter", "len", "any", "all",
    "not", "and", "or", "in", "is", "if", "as", "del", "with",
}


def extract_definitions(content: str) -> set:
    try:
        tree = ast.parse(content)
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
    except SyntaxError:
        return set(re.findall(r'^(?:async\s+)?(?:def|class)\s+([a-zA-Z0-9_]+)',
                              content, re.MULTILINE))


def _split_identifier(name: str) -> list:
    """Split camelCase and snake_case into sub-tokens for better BM25 matching."""

    parts = name.split('_')
    result = []
    for part in parts:
        if not part:
            continue

        sub = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+', part)
        result.extend(sub if sub else [part])
    return [p.lower() for p in result if len(p) > 1]


def _tokenize(text: str) -> list:
    tokens = re.findall(r'[a-zA-Z_]\w{2,}', text.lower())
    expanded = []
    for t in tokens:
        expanded.append(t)
        parts = _split_identifier(t)
        if len(parts) > 1:
            expanded.extend(parts)
    return expanded


def bm25_score(query_tokens: list, doc_tokens: list,
               avg_dl: float, num_docs: int, df: dict,
               k1: float = 1.5, b: float = 0.75) -> float:
    dl = len(doc_tokens)
    tf_map: dict = defaultdict(int)
    for t in doc_tokens:
        tf_map[t] += 1

    score = 0.0
    for term in set(query_tokens):
        if term not in tf_map:
            continue
        tf = tf_map[term]
        idf = math.log((num_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
        norm_tf = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1)))
        score += idf * norm_tf
    return score


def extract_local_words(text: str) -> set:
    return set(re.findall(r'[a-zA-Z_]\w{2,}', text)) - PYTHON_STOPWORDS


def extract_called_names(text: str) -> set:
    """Extract names that are actually called as functions/constructors: Foo( or bar.baz("""
    direct = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{1,})\s*\(', text))
    attr = set(re.findall(r'\.([A-Za-z_][A-Za-z0-9_]{1,})\s*\(', text))
    return (direct | attr) - PYTHON_STOPWORDS


def extract_imported_modules(text: str) -> set:
    mods: set = set()
    for m in re.finditer(r'^(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))',
                         text, re.MULTILINE):
        raw = m.group(1) or m.group(2)
        for part in raw.replace(',', ' ').split():
            if part and part != 'as':
                mods.update(part.replace('.', '/').split('/'))
    return {m.lower() for m in mods if len(m) > 1}


def resolve_relative_imports(prefix: str, target_path: str) -> set:
    """Convert `from . import X` / `from .mod import Y` to candidate rel_paths."""
    target_dir = os.path.dirname(target_path).replace("\\", "/")
    dir_parts = target_dir.split('/') if target_dir else []
    resolved = set()

    for m in re.finditer(
        r'^from\s+(\.+)([\w.]*)\s+import\s+([^\n]+)',
        prefix, re.MULTILINE
    ):
        dots = len(m.group(1))
        module = m.group(2).strip()
        names_raw = m.group(3)
        names = [n.strip().split(' ')[0] for n in names_raw.split(',') if n.strip() and n.strip() != '*']

        levels_up = dots - 1
        base_parts = dir_parts[:len(dir_parts) - levels_up] if levels_up < len(dir_parts) else []
        base = '/'.join(base_parts)

        def make_path(parts_list):
            joined = '/'.join(parts_list)
            return joined.replace('/', os.sep)

        if module:
            mod_parts = module.split('.')

            resolved.add(make_path(base_parts + mod_parts) + '.py')

            resolved.add(make_path(base_parts + mod_parts + ['__init__.py']))
        else:

            for name in names:
                resolved.add(make_path(base_parts + [name + '.py']))
                resolved.add(make_path(base_parts + [name, '__init__.py']))

    return resolved


def extract_relevant_section(content: str, target_words: set, max_chars: int = MAX_CHARS_PER_FILE) -> str:
    """Sliding window to find the densest section of relevant identifiers."""
    if len(content) <= max_chars:
        return content

    lines = content.split('\n')
    line_scores = [sum(1 for w in target_words if w in line) for line in lines]

    best_score = -1
    best_start = 0
    cur_chars = 0
    cur_score = 0
    win_start = 0

    for i, line in enumerate(lines):
        cur_chars += len(line) + 1
        cur_score += line_scores[i]

        while cur_chars > max_chars and win_start <= i:
            cur_chars -= len(lines[win_start]) + 1
            cur_score -= line_scores[win_start]
            win_start += 1

        if cur_score > best_score:
            best_score = cur_score
            best_start = win_start

    section_lines = []
    char_count = 0
    for line in lines[best_start:]:
        if char_count + len(line) + 1 > max_chars:
            break
        section_lines.append(line)
        char_count += len(line) + 1

    result = '\n'.join(section_lines)
    if best_start > 0:
        result += "\n# ... [RELEVANT SECTION]"
    return result


def build_repo_cache(repo_path: str) -> dict:
    repo_cache: dict = {}
    all_tokens: list = []

    for root, _, files in os.walk(repo_path):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full_path = os.path.join(root, fn)
            rel_path = os.path.relpath(full_path, repo_path)
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                tokens = _tokenize(content)
                repo_cache[rel_path] = {
                    "content": content,
                    "definitions": extract_definitions(content),
                    "tokens": tokens,
                    "all_words": set(re.findall(r'[a-zA-Z_]\w{2,}', content)),
                }
                all_tokens.append(tokens)
            except Exception:
                continue

    df: dict = defaultdict(int)
    total_len = 0
    for toks in all_tokens:
        total_len += len(toks)
        for t in set(toks):
            df[t] += 1

    repo_cache["__bm25__"] = {
        "df": dict(df),
        "avg_dl": total_len / max(len(all_tokens), 1),
        "num_docs": len(all_tokens),
    }
    return repo_cache


def build_project_map(repo_path: str, max_py_files: int = 50) -> str:
    lines = ["# Project structure"]
    count = 0
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        rel = os.path.relpath(dirpath, repo_path)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        ind = "  " * depth
        lines.append(f"{ind}{os.path.basename(dirpath) if rel != '.' else '.'}/")
        for fn in sorted(filenames):
            if fn.endswith('.py'):
                lines.append(f"{ind}  {fn}")
                count += 1
                if count >= max_py_files:
                    lines.append("  ... (truncated)")
                    return "\n".join(lines)
    return "\n".join(lines)


def get_context(datapoint: dict, repo_cache: dict, repo_path: str) -> tuple:
    prefix = datapoint.get('prefix') or ""
    suffix = datapoint.get('suffix') or ""
    target_path = datapoint.get('path', '')
    modified_files = datapoint.get('modified') or []

    local_text = (
        "\n".join(prefix.split("\n")[-WINDOW_LINES:])
        + "\n"
        + "\n".join(suffix.split("\n")[:WINDOW_LINES])
    )
    target_vocab = extract_local_words(local_text)
    called_names = extract_called_names(local_text)
    high_priority_vocab = called_names | target_vocab

    query_tokens = _tokenize(local_text)
    imported_mods = extract_imported_modules(prefix)
    relative_paths = resolve_relative_imports(prefix, target_path)

    bm25_meta = repo_cache.get("__bm25__", {})
    df = bm25_meta.get("df", {})
    avg_dl = bm25_meta.get("avg_dl", 1)
    num_docs = bm25_meta.get("num_docs", 1)

    target_dir = os.path.dirname(target_path).replace("\\", "/")


    modified_set = set()
    for mf in modified_files:
        modified_set.add(mf.replace("/", os.sep))
        modified_set.add(mf.replace("\\", "/"))

    assigned: set = set()

    tier_rel:    list = []  
    tier_mod:    list = [] 
    tier_samedir: list = [] 
    tier_1_defs: list = []  
    tier_2_imports: list = [] 
    tier_3_bm25: list = []  

    for rel_path, data in repo_cache.items():
        if rel_path.startswith("__"):
            continue
        if rel_path == target_path:
            continue

        rel_norm = rel_path.replace("\\", "/")
        file_dir = os.path.dirname(rel_norm)
        file_stem = os.path.splitext(os.path.basename(rel_norm))[0].lower()
        file_name = os.path.basename(rel_norm)

        def_overlap = len(high_priority_vocab.intersection(data["definitions"]))
        word_overlap = len(target_vocab.intersection(data["all_words"]))
        called_overlap = len(called_names.intersection(data["definitions"]))

   
        if rel_path in relative_paths or rel_norm in relative_paths:
            tier_rel.append((called_overlap * 20 + def_overlap * 10 + word_overlap, rel_path, data["content"]))
            assigned.add(rel_path)
            continue

        if rel_path in modified_set or rel_norm in modified_set:
            tier_mod.append((called_overlap * 20 + def_overlap * 10 + word_overlap, rel_path, data["content"]))
            assigned.add(rel_path)
            continue


        if file_dir == target_dir:

            priority = called_overlap * 20 + def_overlap * 10 + word_overlap
            if file_name == '__init__.py':
                priority += 500
            tier_samedir.append((priority, rel_path, data["content"]))
            assigned.add(rel_path)
            continue

        if file_name == '__init__.py' and target_dir.startswith(file_dir) and file_dir != target_dir:
            tier_samedir.append((200 + word_overlap, rel_path, data["content"]))
            assigned.add(rel_path)
            continue


        if def_overlap > 0:
            score = called_overlap * 20 + def_overlap * 10 + word_overlap
            tier_1_defs.append((score, rel_path, data["content"]))
            continue


        if imported_mods and (
            file_stem in imported_mods
            or any(m in rel_norm.lower() for m in imported_mods if len(m) > 2)
        ):
            tier_2_imports.append((word_overlap, rel_path, data["content"]))
            continue

       
        score = bm25_score(query_tokens, data["tokens"], avg_dl, num_docs, df)
        if score > 0:
            tier_3_bm25.append((score + word_overlap * 0.1, rel_path, data["content"]))

    tier_rel.sort(    key=lambda x: x[0], reverse=True)
    tier_mod.sort(    key=lambda x: x[0], reverse=True)
    tier_samedir.sort(key=lambda x: x[0], reverse=True)
    tier_1_defs.sort( key=lambda x: x[0], reverse=True)
    tier_2_imports.sort(key=lambda x: x[0], reverse=True)
    tier_3_bm25.sort( key=lambda x: x[0], reverse=True)

    accepted_parts: list = []
    current_chars = 0

    def try_add(rel_path: str, content: str) -> bool:
        nonlocal current_chars
        content = extract_relevant_section(content, high_priority_vocab)
        part = f"File: {rel_path}\n{content}\n{FILE_SEP_SYMBOL}\n"
        if current_chars + len(part) <= MAX_CONTEXT_CHARS:
            accepted_parts.append(part)
            current_chars += len(part)
            return True
        return False

    for tier in (tier_rel, tier_mod, tier_samedir, tier_1_defs, tier_2_imports, tier_3_bm25):
        for _, rel_path, content in tier:
            if current_chars >= MAX_CONTEXT_CHARS:
                break
            try_add(rel_path, content)

    if current_chars < MAX_CONTEXT_CHARS:
        pmap = build_project_map(repo_path)
        map_part = f"File: __project_map__\n{pmap}\n{FILE_SEP_SYMBOL}\n"
        if current_chars + len(map_part) <= MAX_CONTEXT_CHARS:
            accepted_parts.append(map_part)

    accepted_parts.reverse()

    return "".join(accepted_parts), len(accepted_parts)


def expand_repo_path(repo_path_hash: str, language: str, stage: str) -> str:
    base = "data"
    variations = [
        f"repositories-{language}-{stage}",
        f"{language}-{stage}",
        f"repositories-{language}-dataset",
    ]
    for var in variations:
        candidate = os.path.join(base, var, repo_path_hash)
        if os.path.isdir(candidate):
            return candidate

    for var in variations:
        zip_candidate = os.path.join(base, var, repo_path_hash + ".zip")
        if os.path.isfile(zip_candidate):
            extract_dir = os.path.join(base, var, repo_path_hash)
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_candidate, 'r') as z:
                z.extractall(extract_dir)
            logger.info(f"Extracted {zip_candidate}")
            return extract_dir

    return os.path.join(base, f"repositories-{language}-{stage}", repo_path_hash)


def process_datapoint(payload: dict):
    datapoint = payload['datapoint']
    repo_cache = payload['repo_cache']
    repo_path  = payload['repo_path']
    trim_p     = payload['trim_prefix']
    trim_s     = payload['trim_suffix']

    prefix = datapoint.get('prefix') or ""
    suffix = datapoint.get('suffix') or ""
    datapoint['prefix'] = prefix
    datapoint['suffix'] = suffix

    try:
        context_str, files_included = get_context(datapoint, repo_cache, repo_path)
        submission = {"context": context_str}
        if trim_p:
            submission["prefix"] = "\n".join(prefix.split("\n")[-15:])
        if trim_s:
            submission["suffix"] = "\n".join(suffix.split("\n")[:15])
        return submission, files_included
    except Exception as e:
        logger.error(f"CRASH on {datapoint.get('path', 'unknown')}: {repr(e)}")
        submission = {"context": f"Project Map: Error\n{FILE_SEP_SYMBOL}\n"}
        if trim_p:
            submission["prefix"] = "\n".join(prefix.split("\n")[-15:])
        if trim_s:
            submission["suffix"] = "\n".join(suffix.split("\n")[:15])
        return submission, 0


def main():
    stage, language, strategy, limit = args.stage, args.lang, args.strategy, args.limit

    completion_points_file = os.path.join("data", f"{language}-{stage}.jsonl")
    predictions_file = os.path.join("predictions", f"{language}-{stage}-{strategy}.jsonl")
    os.makedirs("predictions", exist_ok=True)

    raw_datapoints = []
    with jsonlines.open(completion_points_file, 'r') as reader:
        for i, dp in enumerate(reader):
            if limit and i >= limit:
                break
            raw_datapoints.append(dp)

    total_datapoints = len(raw_datapoints)
    if total_datapoints == 0:
        return

    logger.info("Caching repositories (AST + BM25 + same-dir + modified)...")
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
            'repo_path': root_directory,
            'trim_prefix': args.trim_prefix,
            'trim_suffix': args.trim_suffix,
        })

    results = []
    total_files_appended = 0

    logger.info("Scoring and assembling context (improved v3)...")
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for count, (sub, files_included) in enumerate(executor.map(process_datapoint, payloads), 1):
            if sub:
                results.append(sub)
            total_files_appended += files_included
            if count % 10 == 0:
                logger.info(f"Processed {count}/{total_datapoints} datapoints.")

    logger.info(f"Writing {len(results)} predictions to {predictions_file}")
    with jsonlines.open(predictions_file, 'w') as writer:
        for res in results:
            writer.write(res)

    print(f"\n{'='*40}\n      IMPROVED ENGINE v3 REPORT\n{'='*40}")
    print(f"Total Datapoints: {total_datapoints}")
    print(f"Average Files Injected: {total_files_appended / total_datapoints:.2f}")
    print(f"{'='*40}\n")


if __name__ == "__main__":
    main()
