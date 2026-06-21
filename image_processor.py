from PIL import Image


def apply_frame(screenshot_path, output_path="framed_output.png"):
    frame_path = "frame.png"

    LEFT = 35
    TOP = -27
    WIDTH = 1287
    HEIGHT = 775

    frame = Image.open(frame_path).convert("RGBA")
    screenshot = Image.open(screenshot_path).convert("RGBA")

    screenshot = screenshot.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    canvas.paste(screenshot, (LEFT, TOP))

    final = Image.alpha_composite(canvas, frame)

    final.save(output_path)

    return output_path
