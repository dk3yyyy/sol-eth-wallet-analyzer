from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_render_blueprint_uses_hardened_production_service_contract():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

    expected_lines = {
        "type: web",
        "name: chainscope-wallet-analyzer",
        "runtime: docker",
        "repo: https://github.com/dk3yyyy/sol-eth-wallet-analyzer",
        "branch: main",
        "plan: free",
        "region: frankfurt",
        "dockerfilePath: ./Dockerfile",
        "dockerContext: .",
        "dockerCommand: uvicorn web_app:app --host 0.0.0.0 --port 10000",
        "healthCheckPath: /api/health",
        "autoDeployTrigger: checksPass",
    }

    configured_lines = {
        line.strip().removeprefix("- ")
        for line in blueprint.splitlines()
        if line.strip()
    }
    assert expected_lines <= configured_lines
    assert "sync: false" not in blueprint
    assert "value:" not in blueprint
