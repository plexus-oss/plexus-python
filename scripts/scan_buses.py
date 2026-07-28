from smbus2 import SMBus

for bus in [0, 1, 10, 20, 21, 22]:
    try:
        s = SMBus(bus)
        addrs = []
        for a in range(0x03, 0x78):
            try:
                s.read_byte(a)
                addrs.append(f'0x{a:02X}')
            except OSError:
                pass
        s.close()
        if addrs:
            print(f'Bus {bus}: {addrs}')
    except Exception as e:
        print(f'Bus {bus}: {e}')
