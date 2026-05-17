import os

path = r'c:\Users\kwon\Desktop\0416 (1)\gui_overlay.py'
with open(path, 'rb') as f:
    content = f.read()

# We need to find the point where the corruption happened.
# It's inside the on_release method of OverlaySelector.
# The code should look something like:
#         if captured_img:
#             try:
#                 ...
#                 print(...)
#                 self.callback(sx, sy, dx, dy, crop)
#             except Exception as e:
#                 print(f"[Error] ROI Capture failed: {e}")
#         self.destroy()
# 
# class ROIIndicator(tk.Toplevel):

target_b = b'class ROIIndicator(tk.Toplevel):'
parts = content.split(target_b)

if len(parts) > 1:
    prefix = parts[0]
    suffix = b'\n' + target_b + parts[1]
    
    # Check if prefix ends with the incomplete try block
    if b'print(f"[DEBUG]' in prefix:
        # We need to close the try/if blocks and destroy the window
        restoration = b'''                if self.callback:
                    self.callback(sx, sy, dx, dy, crop)
            except Exception as e:
                print(f"[Overlay] Capture error: {e}")
        self.destroy()
'''
        # Clean up the prefix if it has trailing incomplete lines
        # Actually, my previous fix script might have already changed line 170.
        # Let's see the current state of line 170.
        lines = prefix.splitlines()
        last_line = lines[-1]
        if b'print' in last_line:
            # OK, it's at the end of the print line.
            new_prefix = b'\n'.join(lines) + b'\n' + restoration
            new_content = new_prefix + suffix
            with open(path, 'wb') as f:
                f.write(new_content)
            print("Successfully restored gui_overlay.py structure")
        else:
            print("Could not find print statement at end of prefix")
    else:
        print("Could not find print statement in prefix")
else:
    print("Could not find class ROIIndicator")
