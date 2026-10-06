"""Run explicitly on the user's machine. Creates an isolated optional AI environment."""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import venv
import zipfile
import shutil
from pathlib import Path
from download_support import certificate_bundles, https_context, download_error

ROOT=Path(__file__).resolve().parent
DDCOLOR_REV='2adb63f2656ac41cbdf7b894cddd94121a3faf13'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--cpu',action='store_true');args=parser.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11 for the optional AI environment.')
    env=ROOT/'.ai-env'
    if not env.exists():venv.EnvBuilder(with_pip=True).create(env)
    python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    # Python.org macOS builds may lack a CA bundle before the first pip install.
    pip_env=os.environ.copy()
    bundles=certificate_bundles()
    if sys.platform=='darwin' and bundles and 'PIP_CERT' not in pip_env:
        pip_env['PIP_CERT']=str(bundles[0])
    subprocess.run([str(python),'-m','pip','install','--upgrade','pip','setuptools<81','wheel','certifi'],check=True,env=pip_env)
    command=[str(python),'-m','pip','install','torch==2.5.1','torchvision==0.20.1']
    if os.name=='nt':command+=['--index-url','https://download.pytorch.org/whl/'+('cpu' if args.cpu else 'cu121')]
    subprocess.run(command,check=True,env=pip_env)
    subprocess.run([str(python),'-m','pip','install','--no-build-isolation','-r',str(ROOT/'requirements-ai.txt')],check=True,env=pip_env)
    vendor=ROOT/'vendor';vendor.mkdir(exist_ok=True)
    archive=vendor/'ddcolor-source.zip'
    print('Downloading pinned DDColor source...')
    url='https://github.com/piddnad/DDColor/archive/'+DDCOLOR_REV+'.zip'
    try:
        with urllib.request.urlopen(url,timeout=60,context=https_context()) as response, archive.open('wb') as stream:
            shutil.copyfileobj(response,stream)
    except OSError as error:
        archive.unlink(missing_ok=True)
        raise SystemExit(download_error(error,url)) from error
    target=vendor/'DDColor';target.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():continue
            relative=Path(*Path(entry.filename).parts[1:]);path=(target/relative).resolve()
            if not path.is_relative_to(target.resolve()):raise ValueError('Invalid archive path')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(bundle.read(entry))
    archive.unlink();(target/'REVISION').write_text(DDCOLOR_REV)
    subprocess.run([str(python),str(ROOT/'ai_runner.py'),'--probe'],check=True)
    print('AI runtime installed. Restart server.py, then download model weights in Tools.')
if __name__=='__main__':main()
