import numpy as np
from PIL import Image
from svc_map_ocr import MapNameRecognizer
from svc_pattern_matcher import PatternMatcher

paths = {
    "entrance": r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets\c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-4ec1ff02-6ac5-43dd-b7e3-cef9584787f6.png",
    "single": r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets\c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-e40de4ea-a45e-4170-8fc2-3b1fd11c371a.png",
    "compound": r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets\c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-1967a0ee-f31d-4d9f-aa12-562e2c19ac98.png",
}

rec = MapNameRecognizer(digit_patterns=PatternMatcher.AHK_PATTERNS)
for k, p in paths.items():
    arr = np.array(Image.open(p).convert("RGB"))
    text = rec.recognize(arr)
    print(k, "=>", repr(text), "score", round(rec.last_score, 3))
