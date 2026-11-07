"""config_for: per-agent env beats global env beats defaults. Environment is monkeypatched."""
import pytest

from agents.llm import DEFAULT_MODELS, AgentConfig, config_for

AGENTS = ["technical", "sentiment", "fundamentals"]
DEBATERS = ["bull", "bear"]
VARS = [f"TRADING_AGENTS_{s}" for s in ("MODEL", "EFFORT")] + [
    f"TRADING_AGENTS_{a.upper()}_{s}" for a in AGENTS + DEBATERS for s in ("MODEL", "EFFORT")
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize("agent", AGENTS + DEBATERS)
def test_defaults(agent):
    cfg = config_for(agent)
    assert cfg == AgentConfig(model=DEFAULT_MODELS[agent])
    assert cfg.model == "claude-sonnet-5-5" and cfg.effort == "high"


def test_global_beats_default(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", "LOW")
    for agent in AGENTS:
        cfg = config_for(agent)
        assert (cfg.model, cfg.effort) == ("claude-opus-5-5", "low")


def test_per_agent_beats_global(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "global-model")
    monkeypatch.setenv("TRADING_AGENTS_SENTIMENT_MODEL", " sent-model ")
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", "low")
    monkeypatch.setenv("TRADING_AGENTS_SENTIMENT_EFFORT", "Max")
    cfg = config_for("sentiment")
    assert (cfg.model, cfg.effort) == ("sent-model", "max")


def test_each_agent_is_independent(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_TECHNICAL_MODEL", "tech-model")
    monkeypatch.setenv("TRADING_AGENTS_FUNDAMENTALS_EFFORT", "medium")
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "global-model")
    tech, sent, fund = (config_for(a) for a in AGENTS)
    assert (tech.model, tech.effort) == ("tech-model", "high")
    assert (sent.model, sent.effort) == ("global-model", "high")
    assert (fund.model, fund.effort) == ("global-model", "medium")


def test_model_and_effort_resolve_independently(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_FUNDAMENTALS_MODEL", "f-model")
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", "xhigh")
    cfg = config_for("fundamentals")
    assert (cfg.model, cfg.effort) == ("f-model", "xhigh")


@pytest.mark.parametrize(
    "agent", ["", "Technical", "Bull", "Trader", None, "technical ", "bull ", "trader "])
def test_unknown_agent_raises(agent):
    with pytest.raises(ValueError, match="unknown agent"):
        config_for(agent)


@pytest.mark.parametrize("var", ["TRADING_AGENTS_MODEL", "TRADING_AGENTS_SENTIMENT_MODEL"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_model_raises(monkeypatch, var, blank):
    monkeypatch.setenv(var, blank)
    with pytest.raises(ValueError, match="non-empty"):
        config_for("sentiment")


@pytest.mark.parametrize("var", ["TRADING_AGENTS_EFFORT", "TRADING_AGENTS_SENTIMENT_EFFORT"])
@pytest.mark.parametrize("bad", ["extreme", "none", "", "  "])
def test_bad_effort_raises(monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError):
        config_for("sentiment")


def test_other_agents_bad_vars_do_not_affect_this_agent(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_FUNDAMENTALS_EFFORT", "extreme")
    assert config_for("technical").effort == "high"
    with pytest.raises(ValueError):
        config_for("fundamentals")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_valid_effort(monkeypatch, effort):
    monkeypatch.setenv("TRADING_AGENTS_TECHNICAL_EFFORT", effort)
    assert config_for("technical").effort == effort


@pytest.mark.parametrize("agent", DEBATERS)
def test_debater_config_for_resolution(monkeypatch, agent):
    up = agent.upper()
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "global-model")
    assert config_for(agent).model == "global-model"
    monkeypatch.setenv(f"TRADING_AGENTS_{up}_MODEL", "side-model")
    monkeypatch.setenv(f"TRADING_AGENTS_{up}_EFFORT", "Low")
    cfg = config_for(agent)
    assert (cfg.model, cfg.effort) == ("side-model", "low")
    other = "bear" if agent == "bull" else "bull"
    assert config_for(other).model == "global-model"
