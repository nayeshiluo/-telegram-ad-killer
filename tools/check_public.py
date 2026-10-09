"""Reject credential/data files and obvious token literals in tracked project files."""
from pathlib import Path
import re
import subprocess

root=Path(__file__).resolve().parents[1]
r=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True)
if r.returncode==0:
    paths=[root/p for p in r.stdout.decode().split('\0') if p]
else:
    # Exported review packages have no .git directory. Scan all delivered files.
    paths=[p for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
errors=[]
for path in paths:
    name=path.name
    if name in {'config.json','bot-token','ai-key'} or name.startswith('.env') or name.endswith(('.db','.sqlite','.log','.key','.pem')):
        errors.append(str(path.relative_to(root)))
        continue
    text=path.read_text(errors='replace')
    if re.search(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}\b',text) or re.search(r'\bsk-[A-Za-z0-9_-]{32,}\b',text):
        errors.append(str(path.relative_to(root)))
if errors:
    raise SystemExit('Potential credentials/data in: '+', '.join(errors))
print('Public source check passed:',len(paths),'tracked files')
