import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import main


@pytest.fixture(autouse=True)
def reset_user_state(tmp_path, monkeypatch):
    main.known_users = {}
    main.user_count = 0
    monkeypatch.setattr(main, "USER_DATA_FILE", str(tmp_path / "user_data.json"))
    monkeypatch.setattr(main, "ADMIN_CHAT_ID", None)
    monkeypatch.setattr(main, "LOG_CHANNEL_ID", None)
    yield


@pytest.mark.asyncio
async def test_registration_does_not_depend_on_admin_logging():
    user = SimpleNamespace(
        id=123,
        username="tester",
        first_name="Test",
        last_name="User",
        language_code="en",
    )

    await main.ensure_user_registered(SimpleNamespace(), user)

    assert "123" in main.known_users
    assert main.user_count == 1
    data = json.loads(Path(main.USER_DATA_FILE).read_text())
    assert "123" in data["users"]


def test_dotenv_is_loaded_before_services_read_rpc_configuration(tmp_path):
    (tmp_path / ".env").write_text(
        "TELEGRAM_TOKEN=test-token\nETHEREUM_RPC_URL=https://ethereum.example.test\n",
        encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import json,sys; "
        f"sys.path.insert(0, {str(repo)!r}); "
        "import main, services; "
        "print(json.dumps({'telegram': bool(main.TELEGRAM_TOKEN), "
        "'ethereum_rpc': services.ETHEREUM_RPC_URL}))"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"TELEGRAM_TOKEN", "ETHEREUM_RPC_URL"}}

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "telegram": True,
        "ethereum_rpc": "https://ethereum.example.test",
    }


@pytest.mark.asyncio
async def test_wallet_batch_limit_rejects_before_external_calls(monkeypatch):
    addresses = [f"0x{i:040x}" for i in range(main.MAX_WALLETS_PER_REQUEST + 1)]
    message = AsyncMock()
    message.text = "\n".join(addresses)
    update = SimpleNamespace(effective_message=message, effective_user=None)
    context = SimpleNamespace(application=SimpleNamespace())
    analysis = AsyncMock()
    monkeypatch.setattr(main, "create_enhanced_ethereum_analysis", analysis)

    await main.handle_wallet_address(update, context)

    assert "at most" in message.reply_text.await_args.args[0].lower()
    analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_summary_reports_mixed_success_and_failure(monkeypatch):
    addresses = ["0x" + "1" * 40, "0x" + "2" * 40]
    message = AsyncMock()
    message.text = "\n".join(addresses)
    update = SimpleNamespace(effective_message=message, effective_user=None)
    context = SimpleNamespace(application=SimpleNamespace())
    analysis = AsyncMock(
        side_effect=[("report", None), main.ServiceError("Ethereum balance")]
    )
    monkeypatch.setattr(main, "create_enhanced_ethereum_analysis", analysis)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    await main.handle_wallet_address(update, context)

    summary = message.reply_text.await_args.args[0]
    assert "Successful: `1`" in summary
    assert "Failed: `1`" in summary


@pytest.mark.asyncio
async def test_refresh_forces_fresh_provider_data(monkeypatch):
    address = "0x" + "1" * 40
    query = AsyncMock()
    query.data = f"refresh_{address}_ethereum"
    update = SimpleNamespace(callback_query=query, effective_user=None)
    context = SimpleNamespace(application=SimpleNamespace())
    analysis = AsyncMock(return_value=("report", None))
    monkeypatch.setattr(main, "create_enhanced_ethereum_analysis", analysis)

    await main.handle_callback(update, context)

    analysis.assert_awaited_once_with(address, force_refresh=True)


@pytest.mark.asyncio
async def test_telegram_ethereum_report_uses_shared_rpc_services(monkeypatch):
    address = "0x" + "1" * 40
    balance = AsyncMock(return_value=1.25)
    price = AsyncMock(return_value=2_000.0)
    monkeypatch.setattr(main, "get_eth_balance", balance)
    monkeypatch.setattr(main, "get_eth_price", price)

    report, keyboard = await main.create_enhanced_ethereum_analysis(address)

    assert "Enhanced Ethereum Analysis" in report
    assert "1.25" in report
    assert "2,500.00" in report
    assert keyboard is not None
    assert balance.await_args.args[0] == address
    assert balance.await_args.kwargs == {"force_refresh": False}
    assert price.await_args.kwargs == {"force_refresh": False}


@pytest.mark.asyncio
async def test_telegram_solana_report_accepts_logo_enriched_metadata(monkeypatch):
    address = "11111111111111111111111111111111"
    monkeypatch.setattr(main, "get_sol_balance", AsyncMock(return_value=10.0))
    monkeypatch.setattr(main, "get_sol_price", AsyncMock(return_value=150.0))
    monkeypatch.setattr(
        main,
        "get_token_accounts",
        AsyncMock(
            return_value=[
                {
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": "AlphaMint1111111111111111111111111111111",
                                    "tokenAmount": {"uiAmountString": "5"},
                                }
                            }
                        }
                    }
                }
            ]
        ),
    )
    monkeypatch.setattr(
        main,
        "get_token_data_dexscreener",
        AsyncMock(
            return_value={
                "name": "Alpha Token",
                "symbol": "ALPHA",
                "price_usd": 2.0,
                "price_in_sol": 0.02,
                "market_cap": 1_000_000,
                "volume_24h": 50_000,
                "liquidity": 100_000,
                "price_change_24h": 4.2,
                "url": "https://dexscreener.com/solana/alpha",
                "logo_url": "https://cdn.dexscreener.com/cms/images/alpha?format=auto",
                "logo_available": True,
            }
        ),
    )

    header, token_pages, keyboard = await main.create_enhanced_solana_analysis(address)

    assert "Enhanced Solana Analysis" in header
    assert "1,510.00" in header
    assert len(token_pages) == 1
    assert "Alpha Token" in token_pages[0]
    assert keyboard is not None


def test_token_balance_falls_back_to_ui_amount_string():
    token_amount = {"uiAmount": None, "uiAmountString": "123.456"}
    assert main.parse_token_balance(token_amount) == 123.456


def test_token_balance_rejects_non_finite_or_malformed_values():
    assert main.parse_token_balance({"uiAmount": "NaN"}) is None
    assert main.parse_token_balance({"uiAmountString": "invalid"}) is None


@pytest.mark.asyncio
async def test_corrupt_user_data_is_backed_up_before_recovery():
    path = Path(main.USER_DATA_FILE)
    path.write_text("{broken", encoding="utf-8")

    await main.load_user_data()

    assert not path.exists()
    assert len(list(path.parent.glob("user_data.json.corrupt.*"))) == 1


@pytest.mark.asyncio
async def test_run_bot_uses_application_context_lifecycle_once(monkeypatch):
    calls = []

    class FakeUpdater:
        running = True

        async def start_polling(self):
            calls.append("updater.start")

        async def stop(self):
            calls.append("updater.stop")

    class FakeApplication:
        updater = FakeUpdater()
        running = True

        def add_error_handler(self, _handler):
            pass

        def add_handler(self, _handler):
            pass

        async def __aenter__(self):
            calls.append("application.enter")
            return self

        async def __aexit__(self, *_args):
            calls.append("application.exit")

        async def start(self):
            calls.append("application.start")

        async def stop(self):
            calls.append("application.stop")

    fake_application = FakeApplication()

    class FakeBuilder:
        def token(self, _token):
            return self

        def build(self):
            return fake_application

    event = SimpleNamespace(wait=AsyncMock(return_value=None))
    monkeypatch.setattr(main.Application, "builder", lambda: FakeBuilder())
    monkeypatch.setattr(main, "load_user_data", AsyncMock())
    monkeypatch.setattr(main, "print_banner", lambda: None)
    monkeypatch.setattr(main.asyncio, "Event", lambda: event)

    await main.main()

    assert calls == [
        "application.enter",
        "application.start",
        "updater.start",
        "updater.stop",
        "application.stop",
        "application.exit",
    ]
