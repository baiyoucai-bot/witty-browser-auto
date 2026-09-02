"""授权范围内的网络观察、响应体捕获和数据扩展接口。"""

from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.recorder import CdpNetworkRecorder

__all__ = ["CdpNetworkCapture", "CdpNetworkRecorder"]
