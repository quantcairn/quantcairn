# SOXS Range Arbitrage Handoff

## 现状
- 已修复 yfinance 价格抓取失败回退逻辑，解决 DRIP 报价失效问题。
- `src/data/fetcher.py` 现在支持：
  - 5d/1m 价格历史回退
  - 1d/5m 和 1d/1m 历史回退
  - `max`/1d 日线回退（适用于 delisted/无实时数据情形）
  - `yfinance.info` 预/盘后价格回退（失败时静默降级）
- `src/config/loader.py` 已强化 `live` 模式配置验证：
  - `broker.longbridge.enabled` 为 `true` 时必须提供 `app_key`、`app_secret`、`access_token`
- `tests/test_fetcher.py` 已补充：
  - history fallback mock 支持 `prepost` 参数
  - live 模式长桥凭证验证测试
- `README.md` 已补齐：
  - 测试使用说明
  - 配置加载优先级
  - 监控脚本说明
- `monitor.sh` 和 `health_check.sh` 已检查，可用于日常监控和故障定位。

## 关键文件
- `run.py`：入口脚本，支持 `--config`, `--paper`, `--live`, `--backtest`, `--dashboard`
- `config.sample.yaml`：配置样例
- `src/config/loader.py`：加载与验证配置
- `src/data/fetcher.py`：价格获取与 yfinance 回退策略
- `tests/test_fetcher.py`：基础回归测试
- `run_tests.py`：无 pytest 环境下的测试启动器
- `health_check.sh`：端口与日志健康检查
- `monitor.sh`：快照记录脚本

## 运行验证
- `python run_tests.py` → 成功
- `python -m py_compile src/config/loader.py src/data/fetcher.py tests/test_fetcher.py` → 语法通过
- `python -c "from src.data.fetcher import PriceFetcher; print(PriceFetcher('DRIP').get_quote())"` → 返回有效报价

## 建议后续步骤
1. 在实际生产机器上执行 `bash health_check.sh`，确认端口与 API 可用性。
2. 如果部署到 macOS `launchd`，检查 `config.local.yaml` 是否与环境变量冲突。
3. 若要进一步稳定，建议增加 `yfinance` 之外的备用行情源（如 Alpha Vantage / IEX / Binance data API）。
4. 若有 live 交易意图，先在 `paper` 模式下跑足够多天的历史和实时验证。

## 备注
- 当前脚本已按 `config.local.yaml -> config.yaml -> config.sample.yaml` 顺序加载。
- `monitor.sh` 依赖本地 `http://localhost:8080/api/status`，如果 dashboard 端口变更请同步修改脚本。
