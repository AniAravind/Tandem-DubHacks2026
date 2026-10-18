"""Fail if a Track 3 branch changes files assigned to another track."""
import argparse
import subprocess
import sys

PREFIXES=('core/','merchant/','tandem/','scripts/ops/','docs/operations/','.github/workflows/')
FILES={'Procfile','package.json','package-lock.json','requirements.txt','requirements.lock.txt','.env.example','.python-version'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='origin/main')
    args=parser.parse_args(argv)
    committed=subprocess.check_output(['git','diff','--name-only','--no-renames',args.base+'...HEAD'],text=True).splitlines()
    working=subprocess.check_output(['git','diff','--name-only','HEAD'],text=True).splitlines()
    untracked=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines()
    violations=sorted(p for p in set(committed+working+untracked) if p not in FILES and not p.startswith(PREFIXES))
    if violations:
        print('Out-of-track paths:\n'+'\n'.join(violations))
        return 1
    print('Track 3 file ownership verified.')
    return 0


if __name__=='__main__':
    sys.exit(main())
