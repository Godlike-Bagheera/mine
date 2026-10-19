# -*- coding: utf-8 -*-
"""Определение комплектующих ПК (Windows 10: WMI/PowerShell, Linux: /proc).

Используется шейдерами для подбора «лучшего шейдера для твоего железа»:
  - CPU: model + количество ядер (Win32_Processor);
  - RAM: суммарный объём планок (Win32_ComputerSystem.TotalPhysicalMemory);
  - GPU: модель и видеопамять (Win32_VideoController; для NVIDIA дополнительно
    nvidia-smi, т.к. WMI иногда врёт про VRAM гибридных ноутбуков);
  - диск: SSD/HDD (Get-PhysicalDisk) — влияет на чанки/загрузку, не на FPS.

Все функции устойчивы к ошибкам: любое недоступное поле = None, а не падение.
Результат кэшируется в памяти процесса (железо за время работы скрипта
не меняется).
"""
import re
import shutil
from pathlib import Path

from modules import debug
from modules.platform_utils import IS_WINDOWS, run_cmd

_cache = {}


# ---------- helpers ----------

def _ps(script: str, timeout: int = 25):
    """Запуск PowerShell-скрипта, stdout или None."""
    if not shutil.which("powershell") and not shutil.which("pwsh"):
        return None
    exe = "powershell" if shutil.which("powershell") else "pwsh"
    r = run_cmd([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture=True, timeout=timeout)
    if r.returncode != 0:
        debug.dbg(f"sysinfo.ps rc={r.returncode}: {r.stderr[:200]}", level="WARN")
        return None
    return r.stdout.strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# ---------- CPU ----------

def get_cpu():
    """{'name', 'cores', 'threads'} — кэш результата."""
    if "cpu" in _cache:
        return _cache["cpu"]
    info = {"name": None, "cores": None, "threads": None}
    try:
        if IS_WINDOWS:
            out = _ps("(Get-CimInstance Win32_Processor | "
                      "Select-Object -First 1 Name,NumberOfCores,"
                      "NumberOfLogicalProcessors | "
                      "ForEach-Object {\"$($_.Name)|$($_.NumberOfCores)|"
                      "$($_.NumberOfLogicalProcessors)\"})")
            if out and "|" in out:
                name, cores, threads = out.split("|", 2)
                info["name"] = _norm(name)
                info["cores"] = int(cores) if cores.isdigit() else None
                info["threads"] = int(threads) if threads.isdigit() else None
        else:
            txt = Path("/proc/cpuinfo").read_text(errors="ignore")
            m = re.search(r"model name\s*:\s*(.+)", txt)
            if m:
                info["name"] = _norm(m.group(1))
            info["cores"] = len(re.findall(r"^processor\s*:", txt, re.M)) or None
    except Exception as e:
        debug.dbg_exc(e, "sysinfo.cpu")
    _cache["cpu"] = info
    return info


# ---------- RAM ----------

def get_ram_gb():
    """Полный объём ОЗУ в гигабайтах (округлённый вниз) или None."""
    if "ram" in _cache:
        return _cache["ram"]
    gb = None
    try:
        if IS_WINDOWS:
            out = _ps("[math]::Round((Get-CimInstance Win32_ComputerSystem)"
                      ".TotalPhysicalMemory/1GB)")
            if out:
                gb = float(out.replace(",", "."))
        else:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    gb = int(line.split()[1]) / 1024 / 1024
                    break
    except Exception as e:
        debug.dbg_exc(e, "sysinfo.ram")
    gb = int(gb) if gb else None
    _cache["ram"] = gb
    return gb


# ---------- GPU ----------

# Условные «баллы» архитектуры GPU для оценки мощности шейдеров.
_GPU_TIERS = [
    # (regex, балл 0..10, тип)  — порядок важен: первые совпадения важнее
    (r"NVIDIA.*(RTX\s*40|RTX\s*50)", 10, "nvidia"),
    (r"NVIDIA.*RTX\s*30",            9,  "nvidia"),
    (r"NVIDIA.*RTX\s*20",            7,  "nvidia"),
    (r"NVIDIA.*GTX\s*1[67]\d",       6,  "nvidia"),
    (r"NVIDIA.*GTX\s*(9|10)\d\d",    5,  "nvidia"),
    (r"NVIDIA.*GTX\s*[67]\d\d",      3,  "nvidia"),
    (r"NVIDIA.*GT\s*[4-7]\d\d?\b",   2,  "nvidia"),
    (r"NVIDIA",                      4,  "nvidia"),
    (r"AMD.*(RX\s*[7-9]\d{3}|RX\s*\d{4})", 8, "amd"),
    (r"AMD.*RX\s*[56]\d{3}",         6,  "amd"),
    (r"AMD.*RX\s*[45]\d{2}",         4,  "amd"),
    (r"AMD.*Radeon\s*(Vega|R9|R7)",  3,  "amd"),
    (r"AMD.*Radeon",                 3,  "amd"),
    (r"Intel.*(Arc\s*A[3-7]\d{2})",  6,  "intel"),
    (r"Intel.*Arc",                  5,  "intel"),
    (r"Intel.*(Iris\s*Xe|UHD\s*7\d{2})", 2, "igpu"),
    (r"Intel.*(UHD|HD)\s*(Graphics|6\d{2}|5\d{2}|4\d{2}|P\d|G\d)?", 1, "igpu"),
    (r"Microsoft.*Basic",            0,  "none"),
    (r"llvmpipe|software",           0,  "none"),
]


def _gpu_score(name: str, vram_gb):
    """Балл 0..10 по модели GPU (+ бонус за显 VRAM)."""
    score, kind = 3, "unknown"
    for pat, s, k in _GPU_TIERS:
        if re.search(pat, name, re.I):
            score, kind = s, k
            break
    if vram_gb is not None:
        if vram_gb >= 8:
            score = min(10, score + 1)
        elif vram_gb <= 2 and kind == "igpu":
            score = max(0, score - 1)
    return score, kind


def get_gpus():
    """Список [{'name','vram_gb','score','kind'}] — дискретные первыми."""
    if "gpus" in _cache:
        return _cache["gpus"]
    gpus = []
    try:
        if IS_WINDOWS:
            out = _ps("Get-CimInstance Win32_VideoController | ForEach-Object {"
                      "\"$($_.Name)|$([math]::Round($_.AdapterRAM/1GB,1))\"}")
            if out:
                for line in out.splitlines():
                    line = line.strip()
                    if not line or "|" not in line:
                        continue
                    name, _, vram = line.partition("|")
                    try:
                        # AdapterRAM у >4 ГБ часто врёт (переполнение int32)
                        v = float(vram.replace(",", "."))
                        if v <= 0 or v > 24:
                            v = None
                    except ValueError:
                        v = None
                    name = _norm(name)
                    if not name:
                        continue
                    s, k = _gpu_score(name, v)
                    gpus.append({"name": name, "vram_gb": v,
                                 "score": s, "kind": k})
            # NVIDIA: уточняем реальную VRAM через nvidia-smi
            if shutil.which("nvidia-smi"):
                r = run_cmd(["nvidia-smi", "--query-gpu=name,memory.total",
                             "--format=csv,noheader,nounits"],
                            capture=True, timeout=15)
                if r.returncode == 0:
                    for line in r.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) == 2 and parts[1].isdigit():
                            name = _norm(parts[0])
                            v = round(int(parts[1]) / 1024, 1)
                            for g in gpus:
                                if name.lower() in g["name"].lower() or \
                                   g["name"].lower() in name.lower():
                                    g["vram_gb"] = v
                                    g["score"], g["kind"] = _gpu_score(name, v)
        else:
            r = run_cmd(["lspci"], capture=True, timeout=10)
            for line in (r.stdout or "").splitlines():
                if re.search(r"VGA|3D controller|Display", line, re.I):
                    name = _norm(line.split(": ", 1)[-1])
                    s, k = _gpu_score(name, None)
                    gpus.append({"name": name, "vram_gb": None,
                                 "score": s, "kind": k})
    except Exception as e:
        debug.dbg_exc(e, "sysinfo.gpus")
    # дискретные с наибольшим баллом — первыми
    gpus.sort(key=lambda g: (g["kind"] == "none", -g["score"]))
    _cache["gpus"] = gpus
    return gpus


def best_gpu():
    gpus = get_gpus()
    return gpus[0] if gpus else {"name": "Неизвестно", "vram_gb": None,
                                 "score": 3, "kind": "unknown"}


# ---------- диск ----------

def get_disk_kind():
    """'ssd' | 'hdd' | None."""
    if "disk" in _cache:
        return _cache["disk"]
    kind = None
    try:
        if IS_WINDOWS:
            out = _ps("Get-PhysicalDisk | Sort-Object Size -Descending | "
                      "Select-Object -First 1 MediaType | "
                      "ForEach-Object { $_.MediaType }")
            if out:
                kind = "ssd" if "SSD" in out.upper() else (
                    "hdd" if "HDD" in out.upper() else None)
    except Exception as e:
        debug.dbg_exc(e, "sysinfo.disk")
    _cache["disk"] = kind
    return kind


# ---------- итоговая оценка ----------

def hardware_summary():
    """dict со всем железом + интегральный балл power 0..10 для рекомендаций."""
    cpu = get_cpu()
    ram = get_ram_gb()
    gpu = best_gpu()
    disk = get_disk_kind()

    power = gpu["score"] * 0.6
    # CPU-поправка: современные многопоточные помогают, древние 2 ядра душат
    threads = cpu.get("threads") or 4
    power += min(2.0, threads / 4.0)
    # RAM-поправка
    if ram:
        if ram >= 16:
            power += 1.5
        elif ram >= 8:
            power += 1.0
        elif ram >= 6:
            power += 0.3
        else:
            power -= 1.0
    if disk == "ssd":
        power += 0.5
    power = max(0.0, min(10.0, round(power, 1)))

    tier = ("ultra-light" if power < 2.5 else
            "light" if power < 4 else
            "medium" if power < 6 else
            "heavy" if power < 8 else
            "ultra")

    return {
        "cpu": cpu, "ram_gb": ram, "gpu": gpu, "gpus": get_gpus(),
        "disk": disk, "power": power, "tier": tier,
    }


def format_summary(hw: dict) -> str:
    """Человекочитаемое описание железа для вывода в терминал."""
    lines = []
    c = hw["cpu"]
    lines.append(f"CPU : {c['name'] or 'неизвестно'}"
                 + (f" ({c['cores']} ядер / {c['threads']} потоков)"
                    if c.get("cores") else ""))
    lines.append(f"RAM : {hw['ram_gb'] or '?'} ГБ")
    g = hw["gpu"]
    lines.append(f"GPU : {g['name']}"
                 + (f", видеопамять ~{g['vram_gb']} ГБ" if g.get("vram_gb") else "")
                 + (f" [{g['kind']}]" if g.get("kind") else ""))
    if hw.get("disk"):
        lines.append(f"Диск: {'SSD' if hw['disk'] == 'ssd' else 'HDD'}")
    lines.append(f"Итог: мощность {hw['power']}/10 → класс «{hw['tier']}»")
    return "\n".join(lines)


if __name__ == "__main__":  # быстрая самопроверка: python -m modules.sysinfo
    print(format_summary(hardware_summary()))
