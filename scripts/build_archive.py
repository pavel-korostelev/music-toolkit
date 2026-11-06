#!/usr/bin/env python3
"""Create a source-only archive from a Git checkout or a downloaded source ZIP."""
import argparse
import ast
from pathlib import Path
import re
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES={'agent.py','music_tool.py','rules.json','requirements.txt','Start.command','Install.command',
            '.gitignore','README.md','AGENTS.md','CLAUDE.md','README.txt','VALIDATION.txt','LICENSE'}
SOURCE_DIRS={'engine','scripts','tests','docs','.github'}
PRIVATE_PARTS={'data','.venv','.git','__pycache__','dist','exports','work','private','.pytest_cache'}

def validate_path(name):
    path=Path(name)
    if path.is_absolute() or '..' in path.parts or any(p.casefold() in PRIVATE_PARTS for p in path.parts):
        raise ValueError('Private or unsafe archive path: '+str(name))
    if path.suffix.lower() in {'.log','.zip','.pyc','.pem','.key'} or path.name.startswith('.env'):
        raise ValueError('Private or generated file: '+str(name))
    if path.name in {'auth-request.json','library.json','catalog-progress.json','sorting-result.json','runner.lock'} or path.suffix=='.lock':
        raise ValueError('Runtime file cannot be published: '+str(name))

def validate_content(name,content):
    patterns=[r'/Users/[A-Za-z0-9._-]+/',r'\bgh[pousr]_[A-Za-z0-9]{20,}\b',
              r'\bgithub_pat_[A-Za-z0-9_]{20,}\b',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
              r'(?i)\bBearer\s+[A-Za-z0-9._-]{24,}']
    for pattern in patterns:
        if re.search(pattern,content):raise ValueError('Potential personal path or credential in '+str(name))

def source_paths(root):
    try:
        top=subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True,check=True).stdout.strip()
        if Path(top).resolve()==root.resolve():
            result=subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,check=True)
            names=[p.decode('utf-8') for p in result.stdout.split(b'\0') if p]
            if names:return names
    except (OSError,subprocess.CalledProcessError):pass
    names=[name for name in ROOT_FILES if (root/name).is_file()]
    for directory in SOURCE_DIRS:
        for path in (root/directory).rglob('*'):
            rel=path.relative_to(root)
            if path.is_file() and not any(p in PRIVATE_PARTS for p in rel.parts):
                names.append(rel.as_posix())
    return sorted(names)

def build(root,output):
    names=source_paths(root)
    for required in ('README.md','AGENTS.md','agent.py','music_tool.py'):
        if required not in names:raise ValueError('Required source is missing: '+required)
    for name in names:
        validate_path(name)
        path=root/name
        if path.is_symlink() or not path.is_file():raise ValueError('Missing or symlinked source: '+name)
        validate_content(name,path.read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for name in sorted(names):z.write(root/name,'music-toolkit/'+name)
    with zipfile.ZipFile(output) as z:
        if z.testzip() is not None:raise ValueError('Archive CRC verification failed')
    return names

def version(root):
    tree=ast.parse((root/'music_tool.py').read_text(encoding='utf-8'))
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or ROOT/'dist'/f'music-toolkit-{version(ROOT)}.zip'
    names=build(ROOT,output)
    print(f'{output}: {len(names)} source files; runtime data excluded')
