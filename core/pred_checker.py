import json

def validate_submission(filepath):
    errors = 0
    total = 0
    
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            total += 1
            try:
                data = json.loads(line)
                
                # Check for mandatory 'context' key
                if "context" not in data:
                    print(f"❌ Line {i}: Missing 'context' field.")
                    errors += 1
                
                # Check for <|file_sep|> if context is non-empty
                ctx = data.get("context", "")
                if ctx and "<|file_sep|>" not in ctx:
                    # Note: Only a warning if your repo has only 1 file
                    print(f"⚠️ Line {i}: No <|file_sep|> found. Ensure files are separated correctly.")
                
                if not ctx.strip():
                    print(f"⚠️ Line {i}: Context is empty.")

            except json.JSONDecodeError:
                print(f"❌ Line {i}: Invalid JSON format.")
                errors += 1

    print(f"\nScan Complete: {total} lines checked, {errors} critical errors found.")

validate_submission("predictions/python-test-slayer.jsonl")