# Live observation (read-only), 2026-09-15

All commands were reads over the vendor telnet. No writes, no actuating pktool subcommands, no ttyS3 access, no process signals.

## Processes (stock, running)
    1 {linuxrc} init (BusyBox), telnetd -F (pid 145, from /etc/init.d/S50telnet), getty on console, crond, syslogd, klogd, axsyslogd, axklogd
    wpa_supplicant -D nl80211 -i wlan0 -c /tmp/wpa_supplicant.conf ; udhcpc -i wlan0
    ./watchdog(199) ./ble(200) ./media(201) ./ctrl(214) ./agora(268) ./cloud(269) ./logUpload(270)
    kernel: spi4 (SPI NAND), ubi/ubifs bgt, loop0-3, npu_*, isp_irq_task_*, vin_nor_sch_*, vpp/gdc/tdp irq threads, RTW_CMD_THREAD (Realtek wifi), watchdogd

## Message bus — RESOLVED
/dev/mqueue is not mounted, but /proc/<pid>/fd shows the POSIX mqueue names: `/msg_dispatch_N`.
    watchdog : /msg_dispatch_5
    ble      : /msg_dispatch_1 /msg_dispatch_2 /msg_dispatch_8
    media    : /msg_dispatch_1 /msg_dispatch_2
    ctrl     : /msg_dispatch_1 /msg_dispatch_2 /msg_dispatch_7 /msg_dispatch_8
    agora    : /msg_dispatch_1 /msg_dispatch_7
    cloud    : /msg_dispatch_4
    logUpload: /msg_dispatch_10
Working hypothesis (queue N = inbox of process id N; a process opens its own inbox plus every destination it sends to):
    1=ctrl (everyone reports to it), 2=media, 8=ble, 7=agora, 4=cloud, 5=watchdog, 10=logUpload; 3/6/9 likely card/p2p/tserver-or-pktool.
To confirm: match against src/dst ids in the dispatch_send_msg debug strings, or read the dispatch table (see msg_id task).

## Shared memory
    /dev/shm/config_shm              11952 bytes  = the config_t struct (saved: live/config_shm.bin — CONTAINS DEVICE CREDENTIALS, treat as secret)
    /dev/shm/media_buffer_frame_buf  8389608 bytes = video frame ring written by media
    /dev/shm/sem.media_buffer_reader_4, sem.media_buffer_reader_5 = two frame consumers (probably agora + cloud/alg)
config_shm holds the DECRYPTED config: productKey/deviceName, Alibaba device secret, MQTT brokers
`iot-mqtt-primary-prod.petkt.com:33882` and `iot-mqtt-standby-prod.petkt.com:33882` (+ `<redacted-broker>`, IPs 47.251.2.236 / 47.88.52.254),
API `https://api.petkt.com/6/` (+ `https://47.88.20.79:80/6/`), region us-west-1, DNS list 8.8.8.8 / 199.85.126.10 / 208.67.222.222,
media event types fullVideo/eventImage/highLight/dynamicVideo, tz America/New_York, MAC 94ba06053336, ip 192.168.4.85.

## Boot chain (from the LIVE ramdisk; not extractable offline because kernel.img is encrypted)
/etc/inittab (BusyBox init): mounts, hostname, then /etc/init.d/rcS; respawns getty on console.
/etc/init.d/rcS:
    ubiattach -m 9  -> mount ubi0_0 /param
    ubiattach -m 10 -> mount ubi1_1 /bak, ubi1_2 /opt
    fcrc /opt/soc.img && mount -t squashfs -o offset=64 /opt/soc.img /soc   (else mount /bak/soc.img)
    /soc/scripts/auto_load_all_drv.sh
    run /etc/init.d/S??* (S01syslogd S02klogd S20urandom S40network S50crontabs S50telnet)
    axsyslogd, axklogd
    if [ -e /opt/system_init.sh ]; then /opt/system_init.sh; else /soc/scripts/system_init.sh; fi     <== HOOK 1
/soc/scripts/system_init.sh:
    exports PATH (/app/bin ...) and LD_LIBRARY_PATH (/soc/usr/lib:/soc/lib:/app/bin:/syslib/lib:/app/lib:/opt/syslib:/alg)
    ln -s /opt/app.img /opt/linkapp.img (falls back to /bak/app.img)
    mount -t squashfs -o offset=64 /opt/linkapp.img /app     (on failure: cp /bak/app.img /opt/app.img; sync; reboot)
    if [ -f /opt/app_init.sh ]; then /opt/app_init.sh & exit 0; elif [ -f /app/script/app_init.sh ]; then it &     <== HOOK 2
Neither /opt/system_init.sh nor /opt/app_init.sh exists on the stock device.

=> PERSISTENCE: a first-party agent needs exactly one file, /opt/app_init.sh (or system_init.sh for full control),
   with stock app.img untouched. telnetd is started from the ramdisk BEFORE either hook, so shell access survives
   any mistake in those files. This is very likely what Localkit's D4H2 "install" uses too.

## Hardware nodes
GPIO (exported, live): gpio47 in=1, gpio51 in=1 (inputs: buttons/lid?), gpio50 out=0, gpio53 out=0, gpio59 out=1, gpio60 out=1, gpio97 out=1.
    gpiochips: 4800000.gpio (0-31), 4801000.gpio (32-63), 6000000.gpio (64-95), 6001000.gpio (96-127)
PWM: 6060000.pwm0 / pwmchip0 / pwm1: period 100000 ns, duty 70000 (70%), enabled  (opened by media -> camera light dimming, not motor)
IIO: iio:device0 = soc:adc, in_voltage0..3_raw (4 ADC channels on the SoC)
/dev: ax_* (Axera MSP nodes), ivps_*, adec/aenc/ai/ao, i2c-1/2/6, mtd0-10 (+ro), npu, snd, ttyS*, video?, watchdog
media holds: /dev/npu, ax_venc, ax_jenc, ax_mipi_rx, ivps_*, snd/pcmC0D0c (mic) + pcmC0D1p (speaker), i2c-6 (sensor), /dev/mem
watchdog holds /dev/watchdog (hardware WDT) and runs `echo 3 > drop_caches` every ~32 s.
ble holds /dev/ttyS3.

## /tmp
ble.img (153412 B, md5 <redacted-32-hex>) = the T31 dispenser-MCU firmware fetched for UART OTA (saved: live/ble.img)
config.lock, wpa_supplicant.conf, resolv.conf, io.agora.rtsa_sdk/, attire/, cron/, logs (/var/log -> /tmp)

## Device tooling
BusyBox 1.35.0 with base64 and timeout; no strace/gdb/ltrace.

## Health note
kernel log shows `spi-nand spi4.0: spinand_check_ecc_status ... (err = 3)` — correctable ECC events on the NAND. The stock image backup was timely.

## Still open after live pass
- numeric msg_id values (compiled constants) — recover from the binaries offline (symtab/dispatch table) or from /proc/<pid>/mem
- pktool per-subcommand usage (it logs via axsyslog, prints nothing on stdout)
- which GPIO is which light / which inputs are the button and lid

## Idle time series of config_shm (60 samples @1 Hz, 2026-09-15; file live/config_shm_series_idle_60s.json)
Only 11 bytes changed in 60 s:
    10152-10156  a85b000022 -> 085b000023   (counter / timestamp-like, ~30 ticks)
    10218-10222  04005c1803 -> 04005a1800   (9 ticks)
    10284, 10288, 10296, 10300, 10304        single bytes toggling 0<->1, 32-37 ticks each  (5 = number of live supervised processes;
                                            10292 and 10308 static = the two dead slots card/p2p?)  => candidate watchdog alive-flags
Hypothesis: each supervised process writes 1 to its flag ~every 2 s and watchdog clears it; a replacement agent must set the flags of the
processes it replaces (ctrl, cloud, agora) or take over watchdog. To be confirmed by STUDY-msgids.md.
