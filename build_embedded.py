"""Developer helper: embed UI, region data, and components into the single deployment script."""
import base64
from pathlib import Path
import re
import zlib


def build():
    root = Path(__file__).resolve().parent
    target = root / "record.py"
    source = target.read_text(encoding="utf-8")
    assets = []
    for name, filename in (("REGIONS", "weather_regions.json"), ("UI", "ui.html")):
        encoded = base64.b64encode(zlib.compress((root / filename).read_bytes(), 9)).decode("ascii")
        assets.append("EMBEDDED_" + name + " = (\n" + "\n".join(
            '    "' + encoded[index:index + 100] + '"' for index in range(0, len(encoded), 100)) + "\n)\n")
    blocks = {"ASSETS": "\n".join(assets)}
    for name in ("weather", "public_weather", "ui_server", "desktop_launcher", "desktop_gui"):
        blocks[name.upper()] = (root / "components" / (name + ".py")).read_text(encoding="utf-8")
    for name, value in blocks.items():
        begin, end = "# BEGIN EMBEDDED " + name, "# END EMBEDDED " + name
        pattern = re.escape(begin) + r".*?" + re.escape(end)
        if len(re.findall(pattern, source, flags=re.S)) != 1:
            raise ValueError("Missing/duplicate component marker: " + name)
        source = re.sub(pattern, lambda _: begin + "\n" + value.rstrip() + "\n" + end, source, flags=re.S)
    compile(source, str(target), "exec")
    target.write_text(source, encoding="utf-8", newline="\n")
    print("Built single-file recorder:", len(source.encode("utf-8")), "bytes")


if __name__ == "__main__":
    build()
