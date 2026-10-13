import time
from PIL import Image, ImageChops, ImageStat
from bambu_link import Printer
from snapshot import snapshot

def jog(p, axis, dist, feed):
    p.gcode(["M211 S", "M211 X1 Y1 Z1", "M1002 push_ref_mode", "G91",
             f"G1 {axis}{dist} F{feed}", "M1002 pop_ref_mode", "M211 R"])

def diff(a, b):
    A, B = Image.open(a).convert("L"), Image.open(b).convert("L")
    # right half of the frame (printer hardware), avoids people moving in the room
    box = (A.width // 3, 0, A.width, A.height)
    return ImageStat.Stat(ImageChops.difference(A.crop(box), B.crop(box))).mean[0]

p = Printer()
snapshot("x0.jpg")
jog(p, "X", 80, 3000); time.sleep(3.5); snapshot("x_plus.jpg")
jog(p, "X", -80, 3000); time.sleep(3.5); snapshot("x_back.jpg")
snapshot("x_back2.jpg")
print("noise (same pose):", round(diff("x_back.jpg", "x_back2.jpg"), 2))
print("x0 -> x+80:      ", round(diff("x0.jpg", "x_plus.jpg"), 2))
print("x+80 -> back:    ", round(diff("x_plus.jpg", "x_back.jpg"), 2))
print("x0 -> back:      ", round(diff("x0.jpg", "x_back.jpg"), 2))
p.close()
