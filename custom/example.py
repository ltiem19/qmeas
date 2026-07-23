# example.py — a qmeas script-device example.
#
# Scripts are plain Python files in the custom/ folder next to
# qmeas.py; each file appears as a command under the 'script' device
# and runs via exec() when its task row executes. Nothing is injected:
# use ordinary Python. Output goes to the console qmeas was started
# from. A script that raises aborts the run (with magnet onhold).

print("Hello from the qmeas example script!")
