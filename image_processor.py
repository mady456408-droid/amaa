from PIL import Image


def apply_frame(screenshot_path, output_path="framed_output.png"):
    frame_path = "frame.png"

    LEFT = 35
    TOP = -29
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


def apply_frame_top_aligned(image_path, output_path="framed_custom.png"):
    """
    Apply frame to custom image with top-aligned fitting behavior.
    
    Frame dimensions remain fixed (1359x875).
    Image slot dimensions remain fixed (1287x775).
    
    Image fitting rules:
    - Image is always top-aligned within the slot.
    - Aspect ratio is preserved.
    - If image becomes taller than slot after scaling: crop from BOTTOM only.
    - If image becomes shorter than slot after scaling: fill BOTTOM with white (#FFFFFF).
    - Never center vertically.
    """
    frame_path = "frame.png"

    LEFT = 35
    TOP = -29
    SLOT_WIDTH = 1287
    SLOT_HEIGHT = 775

    frame = Image.open(frame_path).convert("RGBA")
    image = Image.open(image_path).convert("RGBA")

    # Calculate scaling to fit width to slot width, preserving aspect ratio
    original_width, original_height = image.size
    scale_factor = SLOT_WIDTH / original_width
    scaled_width = SLOT_WIDTH
    scaled_height = int(original_height * scale_factor)

    # Resize image to fit slot width
    image_scaled = image.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

    # Create canvas at slot dimensions
    canvas = Image.new("RGBA", (SLOT_WIDTH, SLOT_HEIGHT), (255, 255, 255, 255))

    if scaled_height >= SLOT_HEIGHT:
        # Image is taller or equal to slot - crop from BOTTOM only
        # Paste at top (y=0), excess will be cropped by canvas size
        canvas.paste(image_scaled, (0, 0))
    else:
        # Image is shorter than slot - top-align and fill bottom with white
        # Paste at top (y=0), white fill already present at bottom
        canvas.paste(image_scaled, (0, 0))

    # Create final canvas at frame dimensions
    final_canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    final_canvas.paste(canvas, (LEFT, TOP))

    # Composite the frame on top
    final = Image.alpha_composite(final_canvas, frame)

    final.save(output_path)

    return output_path
