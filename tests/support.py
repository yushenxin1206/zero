"""测试用最小配置：不依赖磁盘上的 config.json。"""

from zero.config import Config


def make_test_config() -> Config:
    return Config.model_validate({
        "models": {
            "test-model": {
                "provider": "deepseek",
                "contextWindowTokens": 65536,
            },
        },
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "test-model"}}},
        "providers": {
            "deepseek": {"apiBase": "https://test.local", "apiKey": "test"},
        },
    })
