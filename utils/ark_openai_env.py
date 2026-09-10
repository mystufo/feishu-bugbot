import os
from typing import Tuple


def get_ark_openai_config() -> Tuple[str, str, str]:
    """
    读取配置文件中的 API Key、Base URL、模型 Endpoint ID。

    Returns:
        (api_key, base_url, model_endpoint_id)

    Raises:
        ValueError: 缺少必填项时抛出。
    """
    api_key = os.getenv("ARK_API_KEY")
    base_url = os.getenv("ARK_BASE_URL")
    model = os.getenv("ARK_MODEL")
    if not api_key or not base_url or not model:
        raise ValueError(
            "请配置 ARK_API_KEY， ARK_BASE_URL 和 ARK_MODEL"
        )
    return api_key.strip(), base_url.strip(), model.strip()
