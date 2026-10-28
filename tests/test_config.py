"""設定檔位置：由使用者指定的資料夾讀寫 config.json。"""

from nextronics_plc_ver5.config import CONFIG_PATH, AppConfig, config_path, load_config, save_config, set_config_dir


def test_config_follows_user_chosen_dir(tmp_path):
    locator = tmp_path / "home" / "config_location.txt"
    assert config_path(locator) == CONFIG_PATH          # 未指定過 → 預設位置

    chosen = tmp_path / "我的設定"
    set_config_dir(chosen, locator)
    path = config_path(locator)
    assert path == chosen / "config.json"

    cfg = AppConfig()
    cfg.plc.host = "10.0.0.5"
    save_config(cfg, path)
    assert load_config(path).plc.host == "10.0.0.5"
