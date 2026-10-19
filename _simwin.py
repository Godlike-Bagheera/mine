# Симуляция запуска на Windows 10 (для проверки пути IS_WINDOWS под Linux)
import os, sys
sys.path.insert(0, '.')
os.name = 'nt'
os.environ['USERPROFILE'] = '/tmp/fakehome'
os.environ['APPDATA'] = '/tmp/fakehome/AppData/Roaming'
os.makedirs(os.environ['APPDATA'], exist_ok=True)
import modules.platform_utils as pu
assert pu.IS_WINDOWS is True, "IS_WINDOWS should be True"
# entry point
try:
    import wine
except SystemExit as e:
    print("SystemExit:", e.code); sys.exit(0)
from modules.ui import main as ui_main
sys.argv = ['wine.py']
try:
    ui_main()
except SystemExit as e:
    print("ui main exited:", e.code)
