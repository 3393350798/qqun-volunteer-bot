import cv2
import numpy as np
import pytest

from src.qr import decode_qr


@pytest.mark.parametrize("url", ["https://qm.qq.com/q/test", "https://weixin.qq.com/g/test"])
def test_standard_group_qr_from_chinese_path(tmp_path, url):
    code = cv2.QRCodeEncoder_create().encode(url)
    code = cv2.copyMakeBorder(code, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    code = cv2.resize(code, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    path = tmp_path / "群二维码.png"
    path.write_bytes(cv2.imencode(".png", code)[1].tobytes())
    assert decode_qr(str(path)) == [url]


def test_no_qr_returns_empty(tmp_path):
    path = tmp_path / "blank.png"
    path.write_bytes(cv2.imencode(".png", np.full((600, 900, 3), 255, dtype=np.uint8))[1].tobytes())
    assert decode_qr(str(path)) == []


def test_decode_timeout_does_not_prevent_next_scan(tmp_path):
    path = tmp_path / "blank.png"
    path.write_bytes(cv2.imencode(".png", np.zeros((100, 100, 3), dtype=np.uint8))[1].tobytes())
    with pytest.raises(TimeoutError, match="继续监听"):
        decode_qr(str(path), timeout=0.001)
    assert decode_qr(str(path)) == []
