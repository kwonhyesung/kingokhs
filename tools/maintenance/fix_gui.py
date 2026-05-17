import os

path = r'c:\Users\kwon\Desktop\0416 (1)\gui_overlay.py'
with open(path, 'rb') as f:
    lines = f.read().splitlines()

# find the line that contains "class ROIIndicator"
target_idx = -1
for i, line in enumerate(lines):
    if b'class ROIIndicator' in line:
        target_idx = i
        break

if target_idx != -1:
    print(f"Found target at line {target_idx + 1}")
    # Fix the corrupted line
    # The corrupted line looks like: print(f"[DEBUG] ... | 罹class ROIIndicator(tk.Toplevel):
    # We want to split it.
    line_content = lines[target_idx]
    if b'print' in line_content and b'class ROIIndicator' in line_content:
        fixed_print = b'                print(f"[DEBUG] ROI: ({sx},{sy})-({dx},{dy}) | Adjusted: ({csx},{csy})-({cdx},{cdy})")'
        fixed_class = b'class ROIIndicator(tk.Toplevel):'
        lines[target_idx] = fixed_print
        lines.insert(target_idx + 1, fixed_class)
        
        with open(path, 'wb') as f:
            f.write(b'\r\n'.join(lines))
        print("Successfully fixed gui_overlay.py")
else:
    print("Could not find target line")
