"""配置的加载、校验、保存与模板生成。

配置使用 JSON 格式：Python 3.10 没有内置 tomllib，JSON 可以零依赖地读写，
同时 GUI 也容易直接序列化。相对路径统一以程序根目录（打包后为 exe 所在目录）为基准。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    """配置加载或校验失败。"""


# 狭缝函数名 -> HAPI 中的 SLIT_* 常量名
SLIT_FUNCTIONS = {
    "gaussian": "SLIT_GAUSSIAN",
    "rectangular": "SLIT_RECTANGULAR",
    "triangular": "SLIT_TRIANGULAR",
    "michelson": "SLIT_MICHELSON",
    "dispersion": "SLIT_DISPERSION",
    "cosinus": "SLIT_COSINUS",
    "diffraction": "SLIT_DIFFRACTION",
}


@dataclass
class Molecule:
    name: str
    M: int
    I: int


@dataclass
class BandConfig:
    wavelength_min_um: float = 8.9
    wavelength_max_um: float = 11.0


@dataclass
class SimulationConfig:
    wavenumber_step: float = 0.01
    instrument_resolution: float = 0.5
    path_length_cm: float = 1_000_000.0
    temperature_k: float = 296.0
    pressure_atm: float = 1.0
    wavenumber_wing: float = 25.0
    tau_line_min: float = 1e-4
    slit_function: str = "gaussian"


@dataclass
class NetworkConfig:
    use_proxy: bool = False
    proxy: str = "http://127.0.0.1:7890"
    api_key: str = ""
    timeout_s: float = 120.0
    max_retries: int = 3


@dataclass
class PathsConfig:
    cache_dir: str = "hitran_data_cache"
    output_dir: str = "output"


@dataclass
class OutputConfig:
    timestamped_subdir: bool = True
    save_png: bool = True
    save_csv: bool = True
    save_txt: bool = True
    png_dpi: int = 300


@dataclass
class Config:
    band: BandConfig
    simulation: SimulationConfig
    molecules: List[Molecule]
    components: Dict[str, float]
    diluent: Dict[str, float]
    network: NetworkConfig
    paths: PathsConfig
    output: OutputConfig

    @property
    def wavenumber_min(self) -> float:
        """波数下界 (cm^-1)，由最大波长换算。"""
        return 1e4 / self.band.wavelength_max_um

    @property
    def wavenumber_max(self) -> float:
        """波数上界 (cm^-1)，由最小波长换算。"""
        return 1e4 / self.band.wavelength_min_um

    @property
    def slit_function_name(self) -> str:
        if self.simulation.slit_function not in SLIT_FUNCTIONS:
            raise ConfigError(
                "未知的狭缝函数 %r，可选: %s"
                % (self.simulation.slit_function, ", ".join(SLIT_FUNCTIONS))
            )
        return SLIT_FUNCTIONS[self.simulation.slit_function]


def get_base_dir() -> Path:
    """程序根目录：打包后为 exe 所在目录，否则为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / (".write_test_%d.tmp" % os.getpid())
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def get_data_dir(base_dir: Optional[Path] = None) -> Path:
    """可写的数据根目录：优先程序/项目所在目录，不可写则回退到 %LOCALAPPDATA%。"""
    base_dir = base_dir or get_base_dir()
    if _is_writable_dir(base_dir):
        return base_dir
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hapi-transmittance"
    try:
        local.mkdir(parents=True, exist_ok=True)
    except Exception:
        return base_dir
    return local


def get_config_path(base_dir: Optional[Path] = None) -> Path:
    """配置文件的默认路径（位于可写的数据根目录下）。"""
    return get_data_dir(base_dir) / "config.json"


def _resolve(base_dir: Path, value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = base_dir / p
    return p


def get_cache_dir(cfg: Config, base_dir: Optional[Path] = None) -> Path:
    return _resolve(get_data_dir(base_dir), cfg.paths.cache_dir)


def get_output_dir(
    cfg: Config,
    base_dir: Optional[Path] = None,
    override: Optional[str] = None,
) -> Path:
    if override:
        return _resolve(get_data_dir(base_dir), override)
    return _resolve(get_data_dir(base_dir), cfg.paths.output_dir)


def default_config() -> Config:
    return Config(
        band=BandConfig(),
        simulation=SimulationConfig(),
        molecules=[
            Molecule("H2O", 1, 1),
            Molecule("CO2", 2, 1),
            Molecule("O3", 3, 1),
            Molecule("N2O", 4, 1),
            Molecule("CH4", 6, 1),
        ],
        components={
            "H2O": 0.01,
            "CO2": 0.0004,
            "O3": 1e-6,
            "N2O": 3e-7,
            "CH4": 1.8e-6,
        },
        diluent={"air": 1.0},
        network=NetworkConfig(),
        paths=PathsConfig(),
        output=OutputConfig(),
    )


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _deep_merge(base.get(key), value) if key in base else value
        return out
    return override


def _as_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError("字段 %s 必须是数字，收到: %r" % (field_name, value))


def _as_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError("字段 %s 必须是整数，收到: %r" % (field_name, value))


def _build_config(raw: Dict[str, Any]) -> Config:
    band = raw.get("band", {})
    sim = raw.get("simulation", {})
    net = raw.get("network", {})
    paths = raw.get("paths", {})
    out = raw.get("output", {})

    molecules_raw = raw.get("molecules", [])
    if not isinstance(molecules_raw, list):
        raise ConfigError("molecules 必须是数组")
    molecules = []
    for item in molecules_raw:
        if not isinstance(item, dict) or "name" not in item:
            raise ConfigError("molecules 中的每一项必须是包含 name/M/I 的对象")
        molecules.append(
            Molecule(
                name=str(item["name"]),
                M=_as_int(item.get("M"), "molecule.M"),
                I=_as_int(item.get("I"), "molecule.I"),
            )
        )

    components = raw.get("components", {})
    if not isinstance(components, dict):
        raise ConfigError("components 必须是对象")
    components = {str(k): _as_float(v, "components.%s" % k) for k, v in components.items()}

    diluent = raw.get("diluent", {})
    if not isinstance(diluent, dict):
        raise ConfigError("diluent 必须是对象")
    diluent = {str(k): _as_float(v, "diluent.%s" % k) for k, v in diluent.items()}

    cfg = Config(
        band=BandConfig(
            wavelength_min_um=_as_float(
                band.get("wavelength_min_um", 8.9), "band.wavelength_min_um"
            ),
            wavelength_max_um=_as_float(
                band.get("wavelength_max_um", 11.0), "band.wavelength_max_um"
            ),
        ),
        simulation=SimulationConfig(
            wavenumber_step=_as_float(
                sim.get("wavenumber_step", 0.01), "simulation.wavenumber_step"
            ),
            instrument_resolution=_as_float(
                sim.get("instrument_resolution", 0.5),
                "simulation.instrument_resolution",
            ),
            path_length_cm=_as_float(
                sim.get("path_length_cm", 1_000_000.0), "simulation.path_length_cm"
            ),
            temperature_k=_as_float(
                sim.get("temperature_k", 296.0), "simulation.temperature_k"
            ),
            pressure_atm=_as_float(
                sim.get("pressure_atm", 1.0), "simulation.pressure_atm"
            ),
            wavenumber_wing=_as_float(
                sim.get("wavenumber_wing", 25.0), "simulation.wavenumber_wing"
            ),
            tau_line_min=_as_float(
                sim.get("tau_line_min", 1e-4), "simulation.tau_line_min"
            ),
            slit_function=str(sim.get("slit_function", "gaussian")),
        ),
        molecules=molecules,
        components=components,
        diluent=diluent,
        network=NetworkConfig(
            use_proxy=bool(net.get("use_proxy", False)),
            proxy=str(net.get("proxy", "http://127.0.0.1:7890")),
            api_key=str(net.get("api_key", "")),
            timeout_s=_as_float(net.get("timeout_s", 120.0), "network.timeout_s"),
            max_retries=_as_int(net.get("max_retries", 3), "network.max_retries"),
        ),
        paths=PathsConfig(
            cache_dir=str(paths.get("cache_dir", "hitran_data_cache")),
            output_dir=str(paths.get("output_dir", "output")),
        ),
        output=OutputConfig(
            timestamped_subdir=bool(out.get("timestamped_subdir", True)),
            save_png=bool(out.get("save_png", True)),
            save_csv=bool(out.get("save_csv", True)),
            save_txt=bool(out.get("save_txt", True)),
            png_dpi=_as_int(out.get("png_dpi", 300), "output.png_dpi"),
        ),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    problems = []

    if cfg.band.wavelength_min_um <= 0 or cfg.band.wavelength_max_um <= 0:
        problems.append("波长必须大于 0")
    if cfg.band.wavelength_min_um >= cfg.band.wavelength_max_um:
        problems.append("波长下限必须小于上限")
    if cfg.simulation.wavenumber_step <= 0:
        problems.append("wavenumber_step 必须大于 0")
    if cfg.simulation.instrument_resolution <= 0:
        problems.append("instrument_resolution 必须大于 0")
    if cfg.simulation.path_length_cm <= 0:
        problems.append("path_length_cm 必须大于 0")
    if cfg.simulation.temperature_k <= 0:
        problems.append("temperature_k 必须大于 0")
    if cfg.simulation.pressure_atm <= 0:
        problems.append("pressure_atm 必须大于 0")
    if cfg.simulation.slit_function not in SLIT_FUNCTIONS:
        problems.append("slit_function 无效")
    if not cfg.molecules:
        problems.append("molecules 不能为空")
    if not cfg.components:
        problems.append("components 不能为空")
    for name, value in cfg.components.items():
        if value < 0:
            problems.append("成分 %s 的混合比不能为负" % name)
    if sum(cfg.components.values()) > 1.0:
        problems.append("各成分混合比之和超过 1，请检查")
    if cfg.network.timeout_s <= 0:
        problems.append("network.timeout_s 必须大于 0")
    if cfg.network.max_retries < 0:
        problems.append("network.max_retries 不能为负")

    if problems:
        raise ConfigError("；".join(problems))


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("配置文件不存在: %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("配置文件不是合法 JSON (%s): %s" % (path, exc))
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")
    defaults = asdict(default_config())
    merged = _deep_merge(defaults, raw)
    return _build_config(merged)


def save_config(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def ensure_config_exists(path: Path) -> bool:
    """配置文件不存在时生成模板，返回是否新建了文件。"""
    if path.exists():
        return False
    save_config(default_config(), path)
    return True
