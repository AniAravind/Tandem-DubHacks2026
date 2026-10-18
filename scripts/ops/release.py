"""Release gate: validate configuration before applying migrations; never seed/reset data."""
import os
import subprocess
import sys


def main():
    env={**os.environ,'DJANGO_SETTINGS_MODULE':'tandem.settings'}
    for command in [['check','--deploy','--fail-level','ERROR'],['migrate','--noinput']]:
        result=subprocess.run([sys.executable,'manage.py',*command],env=env,check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__=='__main__':
    raise SystemExit(main())
