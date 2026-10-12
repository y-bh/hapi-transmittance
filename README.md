# HAPI 大气透过率仿真

基于 HITRAN 数据库与 HAPI 库，计算指定波段的大气透过率曲线，并提供 GUI、配置文件和日志。

详细使用说明见 [使用说明.md](使用说明.md)。

## 功能

- 从 HITRAN 下载逐线光谱数据并本地缓存，自动复用已下载数据。
- 计算高分辨率 Voigt 吸收系数、透过率谱，并按指定仪器分辨率做狭缝卷积。
- 结果输出到指定目录：PNG 图、CSV、TXT。
- 运行日志写入 `output/logs/`（或 `logs/`），出错时可查看完整堆栈。

## 目录结构

```text
app.py                命令行入口
app_gui.py            GUI 入口
core/
  config.py           配置加载/校验/保存
  logging_setup.py    日志初始化
  fetcher.py          谱线下载与缓存
  simulator.py        吸收系数/透过率/卷积
  exporter.py         结果输出
  pipeline.py         流水线编排
hapi.py               HITRAN 官方 HAPI 库（第三方）
legacy/               旧版单文件脚本（参考）
config.example.json   配置模板
build.spec            PyInstaller 打包配置
```

## 使用（源码方式）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py                # 命令行
python app_gui.py            # 图形界面
```

首次运行会在程序根目录生成 `config.json`。也可以在命令行指定：

```powershell
python app.py --config config.json --output D:/results
```

## 配置说明

配置文件为 JSON，字段含义：

- `band`：波长范围（μm）。
- `simulation`：计算步长、仪器分辨率、光程、温压、谱线翼截断、最小光学厚度、狭缝函数。
- `molecules`：分子清单（name, M, I），对应 HITRAN 分子号与同位素号。
- `components`：各气体体积混合比。
- `diluent`：展宽气体（保持 `air: 1.0`）。
- `network`：代理开关、代理地址、API Key、超时与重试次数。API Key 也可用环境变量 `HITRAN_API_KEY` 提供。
- `paths`：谱线缓存目录与输出目录（相对路径以程序根目录为基准）。
- `output`：是否按时间戳分目录、是否保存 PNG/CSV/TXT、PNG 分辨率。

## 打包为 exe（--onedir）

```powershell
.venv\Scripts\Activate.ps1
pyinstaller build.spec
```

产物在 `dist/hapi_transmittance/`，双击 `hapi_transmittance.exe` 运行。
默认 `console=False`（无黑窗口）。排查问题时可在 `build.spec` 中把 `console` 改为 `True` 重新打包。
配置、缓存、输出默认都放在 exe 所在目录；请勿把程序放进无写权限的目录（如 Program Files）。
