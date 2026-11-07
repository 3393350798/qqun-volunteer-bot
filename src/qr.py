from pathlib import Path
import json
import subprocess
import sys


def decode_qr(path: str, timeout: float = 5) -> list[str]:
    """Decode QR image content; returns zero or more URLs without joining automatically."""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(path)
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), str(source)],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"二维码识别超过 {timeout:g} 秒，已结束本次解码，继续监听") from exc
    if result.returncode:
        raise RuntimeError("二维码解码进程失败：" + result.stderr[-500:])
    return json.loads(result.stdout)


def _decode_image(path: str) -> list[str]:
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(path)
    detector = cv2.QRCodeDetector()
    results = []
    candidates = [image, cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)]
    for candidate in candidates:
        value, _, _ = detector.detectAndDecode(candidate)
        if value:
            results.append(value)
        multi = detector.detectAndDecodeMulti(candidate)
        if multi[0]:
            results.extend(value for value in multi[1] if value)
    return list(dict.fromkeys(results))


if __name__ == "__main__":
    print(json.dumps(_decode_image(sys.argv[1])))
