from pathlib import Path
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parent / "render"
poppler = r"C:\Users\32443\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin"
images = convert_from_path(str(root / "guide.pdf"), dpi=140, poppler_path=poppler)
for stale in root.glob("page-*.png"):
    if stale.stem.removeprefix("page-").isdigit() and int(stale.stem[5:]) > len(images):
        stale.unlink()
thumbs = []
for i, page in enumerate(images, 1):
    page.save(root / f"page-{i}.png")
    thumb = page.copy()
    thumb.thumbnail((425, 550))
    canvas = Image.new("RGB", (465, 590), "#e6e8eb")
    canvas.paste(thumb, ((465 - thumb.width) // 2, 22))
    ImageDraw.Draw(canvas).text((18, 566), f"Page {i}", fill="black")
    thumbs.append(canvas)
for offset in range(0, len(thumbs), 6):
    subset = thumbs[offset:offset + 6]
    sheet = Image.new("RGB", (465 * 3, 590 * 2), "#d8dce0")
    for j, thumb in enumerate(subset):
        sheet.paste(thumb, ((j % 3) * 465, (j // 3) * 590))
    sheet.save(root / f"contact-{offset // 6 + 1}.png")
print(f"Rendered {len(images)} PNG pages")
