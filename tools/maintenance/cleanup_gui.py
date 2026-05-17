import os

path = r'c:\Users\kwon\Desktop\0416 (1)\gui_overlay.py'
with open(path, 'rb') as f:
    lines = f.read().splitlines()

# 1. Clean up ROIIndicator class
# 2. Clean up GridIndicator class initiation

new_lines = []
skip_mode = False

for i, line in enumerate(lines):
    # Detect start of ROIIndicator
    if b'class ROIIndicator(tk.Toplevel):' in line:
        new_lines.append(line)
        # We'll rewrite the whole class from here
        new_lines.append(b'    """Docstring."""')
        new_lines.append(b'    def __init__(self, parent, hwnd, state):')
        new_lines.append(b'        super().__init__(parent)')
        new_lines.append(b'        self.hwnd = hwnd')
        new_lines.append(b'        self.state = state')
        new_lines.append(b'        self.visible = False')
        new_lines.append(b'        ')
        new_lines.append(b'        self.overrideredirect(True)')
        new_lines.append(b'        self.attributes("-topmost", True)')
        new_lines.append(b'        self.attributes("-transparentcolor", "black")')
        new_lines.append(b'        self.configure(bg="black")')
        new_lines.append(b'        ')
        new_lines.append(b'        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)')
        new_lines.append(b'        self.canvas.pack(fill="both", expand=True)')
        new_lines.append(b'        self.withdraw()')
        new_lines.append(b'')
        new_lines.append(b'    def update_position(self, regions):')
        new_lines.append(b'        has_markers = len(self.state.visual_markers) > 0')
        new_lines.append(b'        if not self.visible and not has_markers:')
        new_lines.append(b'            self.withdraw()')
        new_lines.append(b'            return')
        new_lines.append(b'')
        new_lines.append(b'        try:')
        new_lines.append(b'            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))')
        new_lines.append(b'            _, _, w, h = win32gui.GetClientRect(self.hwnd)')
        new_lines.append(b'            self.geometry(f"{w}x{h}+{left}+{top}")')
        new_lines.append(b'            self.deiconify()')
        new_lines.append(b'            self.lift()')
        new_lines.append(b'            self.canvas.delete("all")')
        new_lines.append(b'            ')
        new_lines.append(b'            if self.visible:')
        new_lines.append(b'                colors = {"hp": "#FF4B4B", "mp": "#00D4FF", "exp": "#A855F7", "money": "#FFB800", "x": "#10B981", "y": "#10B981"}')
        new_lines.append(b'                for name, reg in regions.items():')
        new_lines.append(b'                    color = colors.get(name, "#FFFFFF")')
        new_lines.append(b'                    self.canvas.create_rectangle(reg.sx - 3, reg.sy - 3, reg.dx + 3, reg.dy + 3, outline=color, width=2)')
        new_lines.append(b'            ')
        new_lines.append(b'            now = time.time()')
        new_lines.append(b'            markers = []')
        new_lines.append(b'            with self.state._lock:')
        new_lines.append(b'                markers = list(self.state.visual_markers)')
        new_lines.append(b'                self.state.visual_markers = [m for m in self.state.visual_markers if m["expiry"] > now]')
        new_lines.append(b'')
        new_lines.append(b'            for m in markers:')
        new_lines.append(b'                if m["expiry"] > now:')
        new_lines.append(b'                    r = m.get("size", 10) // 2')
        new_lines.append(b'                    color = m.get("color", "red")')
        new_lines.append(b'                    self.canvas.create_oval(m["x"] - r, m["y"] - r, m["x"] + r, m["y"] + r, fill=color, outline="white", width=1)')
        new_lines.append(b'        except Exception as e:')
        new_lines.append(b'            print(f"[Overlay] ROI update error: {e}")')
        new_lines.append(b'            self.withdraw()')
        
        # Now skip until we find GridIndicator
        skip_mode = True
        continue
    
    if skip_mode:
        if b'class GridIndicator(tk.Toplevel):' in line:
            new_lines.append(b'')
            new_lines.append(line)
            new_lines.append(b'    """Docstring."""')
            new_lines.append(b'    def __init__(self, parent, hwnd, state):')
            new_lines.append(b'        super().__init__(parent)')
            new_lines.append(b'        self.hwnd = hwnd')
            new_lines.append(b'        self.state = state')
            new_lines.append(b'        self.visible = False')
            new_lines.append(b'        self.grid_size = 48.2')
            new_lines.append(b'        self.offset_x = 0')
            new_lines.append(b'        self.offset_y = 0')
            new_lines.append(b'        self.grid_alpha = 0.5')
            
            # Skip until we find update_position of GridIndicator or next class
            skip_mode = False
            # We'll resume from a known point or just keep skipping until update_position
            # Actually, let's look for the next method.
            # I'll just skip the current __init__ content.
            # Let's find where update_position starts for GridIndicator.
            found_update = False
            for j in range(i + 1, len(lines)):
                if b'def update_position' in lines[j]:
                    # We'll skip everything until here
                    # But we need to make sure we don't skip TOO much if there's other stuff.
                    # Let's assume the next method is what we want.
                    pass
            # Better: skip until 'def update_position' or next class
            skip_mode = 'grid_init'
        continue

    if skip_mode == 'grid_init':
        if b'def update_position' in line:
            new_lines.append(b'')
            new_lines.append(line)
            skip_mode = False
        continue

    new_lines.append(line)

with open(path, 'wb') as f:
    f.write(b'\r\n'.join(new_lines))
print("Cleaned up gui_overlay.py classes")
