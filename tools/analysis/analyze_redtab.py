import re

ft_string = "|<red_tab>FF5757-0.90$49.zzzzzzzzzzzzzzzzs"
match = re.search(r"<(.*?)>(.*?)\$(\d+)\.(.*)", ft_string)
p_name, p_config, p_width, p_data = match.groups()
p_width = int(p_width)

ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
c2v = {c: i for i, c in enumerate(ahk_chars)}
bits = "".join(f"{c2v.get(ch, 0):06b}" for ch in p_data)
bits = re.sub(r"10*$", "", bits)
p_height = len(bits) // p_width

print(f"Name: {p_name}, Width: {p_width}, Height: {p_height}")
ones = bits.count('1')
zeros = bits.count('0')
print(f"Ones: {ones}, Zeros: {zeros}, Total: {len(bits)}")
print(f"Density: {ones/len(bits):.2%}")
