# Linux-side Bluetooth on the RTL8733BU — feasibility (2026-09-15, read-only probes)

Question: can the Wi-Fi module's built-in Bluetooth be brought up on the Linux SoC to make the feeder an HA Bluetooth proxy?

## Evidence
1. USB descriptor of the module (sysfs `descriptors`, decoded):
   device 12 01 00 02 00 00 00 40 | 0bda:f72b | 1 configuration
   config  09 02 35 00 **01** 01 00 80 fa   -> exactly ONE interface, bus-powered 500 mA
   iface   09 04 00 00 05 ff ff ff        -> vendor-specific (WLAN), 5 endpoints (bulk 0x84 in, 0x05/0x06/0x08 out, intr 0x87)
   A Realtek combo that carries Bluetooth over USB exposes a second interface (class e0/01/01) — absent here. BT is NOT on USB.
2. Realtek 8733B-class parts carry Bluetooth on a separate UART (H5) rather than USB. The SoC has six UARTs; the device tree enables only
   4880000 (ttyS0 console) and 6080000 (ttyS3, dispenser MCU). 4881000/4882000/6081000/6082000 are `status=disabled`. Nothing is wired for a BT UART.
3. Kernel: no bluetooth/hci/rfkill modules in /sys/module, ~0 Bluetooth symbols in /proc/kallsyms, no /proc/config.gz. The kernel has no Bluetooth
   subsystem, and the kernel + device tree live in the ENCRYPTED kernel partition, so neither can be rebuilt or overlaid by us.
4. No /lib/firmware on the device at all; the WLAN driver embeds its own firmware (`rtl8733b_fw_dl`). No BT firmware blob exists on-device.
   (DT nodes `bt_dpi0/1` are Axera display/test interfaces, not Bluetooth.)

## Verdict
Not feasible without hardware work AND a kernel we cannot build: the BT block is not on USB, its UART is not routed/enabled, the kernel lacks the
stack, and the boot images are encrypted. Treat the module as Wi-Fi-only.

## The realistic path for "feeder as a BLE proxy"
The Telink MCU is the only usable BLE radio, and its firmware already acts as a BLE *central* for the Petkit relay feature
(UART cmd 0x15 Relay connect, 0x11 ble trans data; Linux handlers dispatch_handler_ble_set_BLE_relay / WAN_ctrl_ble_relay / discon_ble_relay).
- Feasible: a Petkit-device proxy (fountains, K3) driven by our agent through the existing relay commands.
- Not feasible without rewriting the Telink firmware (TC32, vendor SDK): a general HA Bluetooth proxy (continuous passive scan + raw advertisement
  forwarding + arbitrary GATT), which the MCU firmware almost certainly does not expose.
See STUDY-ble.md for the MCU BLE protocol.
