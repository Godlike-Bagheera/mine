import sys, os
sys.path.insert(0, '.')
mods = ['prefix','shaders','minecraft','sysinfo','config','state','launcher','commands','commands_extra','ui','worlds','java','net','download','winetricks','debug','settings','gamemode','art','gamestate','hash_utils','colors','platform_utils']
bad=[]
for m in mods:
    try: __import__('modules.'+m)
    except Exception as e: bad.append((m,type(e).__name__,str(e)[:150]))
print("MODULE FAILS:", bad if bad else "none")
try:
    import wine
    print("entry wine.py OK")
except SystemExit as e:
    print("entry SystemExit code:", e.code)
except BaseException as e:
    print("entry FAIL:", type(e).__name__, str(e)[:300])
from modules import prefix
need=['choose_minecraft_dir','show_minecraft_paths','ensure_prefix','get_wine_env','find_minecraft_dirs','installed_versions','pick_mc_dir_interactive','latest_used_dir']
print("prefix missing exports:", [n for n in need if not hasattr(prefix,n)])
from modules.gamemode import gamemode_available
print("gamemode_available():", gamemode_available())
