import ast
import os
from collections import Counter

def check_duplicates(file_path):
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return
    
    with open(file_path, 'r', encoding='utf-8') as f:
        try:
            tree = ast.parse(f.read())
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")
            return
        
    found_any = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            method_names = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_names.append(item.name)
            
            counts = Counter(method_names)
            duplicates = [name for name, count in counts.items() if count > 1]
            
            if duplicates:
                found_any = True
                print(f"Duplicates found in class '{node.name}' in file '{file_path}':")
                for d in duplicates:
                    print(f"  - {d} (found {counts[d]} times)")
    
    if not found_any:
        print(f"No duplicate methods found in {file_path}")

if __name__ == "__main__":
    check_duplicates(r'c:\Users\efitz\PycharmProjects\discord-bot-v2\cogs\mlb.py')
    check_duplicates(r'c:\Users\efitz\PycharmProjects\discord-bot-v2\core\mlb_client.py')
