# 実行環境

- 日時: 2026-10-24T06:07:33
- ホスト: zefat / カーネル 7.0.0-38-generic
- マザーボード: ASRock X870 Taichi Creator / BIOS 4.60 (09/17/2026)
- CPU: AMD Ryzen 9 9950X3D 16-Core Processor
- cpufreq ドライバ: amd-pstate-epp / EPP: balance_performance
- GPU: 0, NVIDIA GeForce RTX 4090, 595.99.02, 450.00 W / 1, NVIDIA RTX 4000 Ada Generation, 595.99.02, 130.00 W
- RAPL (CPU パッケージ電力): 取得可
- stress-ng: stress-ng, version 0.20.01 (gcc 15.2.0, x86_64 Linux 7.0.0-38-generic)

## 設定メモ

BIOS (2026-10-24 06:05頃〜): Performance Preset 自動 / PBO Limits Manual (PPT 162W / TDC 120A / EDC 180A) / Curve Optimizer CCD0 (3D V-Cache) -35 / CCD1 -40 / 温度上限 85℃
BIOS (2026-10-24 05:28頃〜, CO なしの比較用): Performance Preset 自動 / PBO Limits Manual (PPT 162W / TDC 120A / EDC 180A) / Curve Optimizer 無効 / 温度上限 85℃
BIOS (2026-10-24 04:48頃〜): CO を無効にしたつもりが、実際は CO CCD0 -30 / CCD1 -35 のまま (02:30〜 と同じ設定の再測定)
BIOS (2026-10-24 02:30頃〜): Performance Preset 自動 / PBO Limits Manual (PPT 162W / TDC 120A / EDC 180A) / Curve Optimizer CCD0 (3D V-Cache) -30 / CCD1 -35 / 温度上限 85℃
BIOS (2026-10-21 23:10頃〜): Performance Preset 自動 / PBO Limits Manual (PPT 162W / TDC 120A / EDC 180A) / Curve Optimizer CCD0 (3D V-Cache) -25 / CCD1 -30 / 温度上限 85℃
BIOS (2026-10-21 13:16頃〜): Performance Preset 自動 / PBO Limits Manual (PPT 142W / TDC 110A / EDC 170A) / Curve Optimizer -20 (PBO の項目) / 温度上限 85℃。CO -40 (PBO の項目) は 13:10 に cpu2 の単コア負荷中にフリーズしたため
BIOS (2026-10-21 12:30頃〜): Performance Preset を「自動」に戻し、PBO Limits Manual (PPT 142W / TDC 110A / EDC 170A)・Curve Optimizer -40・温度上限 85℃ をプリセット以外の項目で設定
BIOS (2026-10-21 12:13〜): ECO モード 105W / PBO Advanced, PBO Limits Manual (PPT 142W / TDC 110A / EDC 170A) / Curve Optimizer 全コア -40 / 温度上限 85℃ (前回から変更なしの前提)
BIOS (2026-10-21 午前): ECO モード 105W / Curve Optimizer 全コア -40 / 温度上限 (Tjmax) 85℃ (前回から変更なしの前提)
BIOS (2026-10-20): TDP 105W 制限 / PBO 有効 / 温度上限 (Tjmax) 85℃ / Curve Optimizer 全コア -30

## 記録した hwmon チャンネル

nvme:nvme0/Composite, nvme:nvme0/Sensor 1, nvme:nvme1/Composite, nvme:nvme1/Sensor 1, nvme:nvme2/Composite, nvme:nvme2/Sensor 1, nct6686/Thermistor 14, nct6686/Thermistor 15, nct6686/AMD TSI Addr 98h, nct6686/Thermistor 16, nct6686/fan1, nct6686/fan2, nct6686/fan3, nct6686/fan4, nct6686/fan5, nct6686/fan6, nct6686/fan7, nct6686/VIN0, nct6686/VTT, nct6686/VBAT, nct6686/VIN1, nct6686/VIN2, nct6686/VIN3, nct6686/VIN5, nct6686/VIN6, nct6686/VIN7, nct6686/VCC, nct6686/VSB, nct6686/AVSB, k10temp/Tctl, k10temp/Tccd1, k10temp/Tccd2, r8169_0_800:00/temp1, enp9s0/PHY Temperature, enp9s0/MAC Temperature, spd5118:2-0051/temp1, spd5118:2-0053/temp1, amdgpu/edge, amdgpu/PPT, amdgpu/vddgfx, amdgpu/vddnb

## sensors (開始時)

```
spd5118-i2c-2-51
Adapter: SMBus PIIX4 adapter port 0 at 0b00
temp1:        +43.5°C  (low  =  +0.0°C, high = +55.0°C)
                       (crit low =  +0.0°C, crit = +85.0°C)

r8169_0_800:00-mdio-0
Adapter: MDIO adapter
temp1:        +52.0°C  (high = +120.0°C)

amdgpu-pci-1500
Adapter: PCI adapter
vddgfx:      914.00 mV 
vddnb:         1.09 V  
edge:         +47.0°C  
PPT:           8.00 mW 
sclk:         600 MHz 

nct6686-isa-0a20
Adapter: ISA adapter
VIN0:             608.00 mV (min =  +0.00 V, max =  +0.00 V)
VIN1:               1.01 V  (min =  +0.00 V, max =  +0.00 V)
VIN2:               1.01 V  (min =  +0.00 V, max =  +0.00 V)
VIN3:               1.70 V  (min =  +0.00 V, max =  +0.00 V)
VIN5:               1.10 V  (min =  +0.00 V, max =  +0.00 V)
VIN6:               1.12 V  (min =  +0.00 V, max =  +0.00 V)
VIN7:               1.12 V  (min =  +0.00 V, max =  +0.00 V)
VCC:                3.36 V  (min =  +0.00 V, max =  +0.00 V)
VSB:                3.42 V  (min =  +0.00 V, max =  +0.00 V)
AVSB:               3.39 V  (min =  +0.00 V, max =  +0.00 V)
VTT:                1.79 V  (min =  +0.00 V, max =  +0.00 V)
VBAT:               3.36 V  (min =  +0.00 V, max =  +0.00 V)
fan1:              870 RPM  (min =    0 RPM)
fan2:                0 RPM  (min =    0 RPM)
fan3:              791 RPM  (min =    0 RPM)
fan4:                0 RPM  (min =    0 RPM)
fan5:                0 RPM  (min =    0 RPM)
fan6:                0 RPM  (min =    0 RPM)
fan7:                0 RPM  (min =    0 RPM)
Thermistor 14:     +54.0°C  (low  =  +0.0°C)
                            (high =  +0.0°C, hyst =  +0.0°C)
                            (crit =  +0.0°C)  sensor = thermistor
Thermistor 15:     +40.5°C  (low  =  +0.0°C)
                            (high =  +0.0°C, hyst =  +0.0°C)
                            (crit =  +0.0°C)  sensor = thermistor
AMD TSI Addr 98h:  +48.0°C  (low  =  +0.0°C)
                            (high =  +0.0°C, hyst =  +0.0°C)
                            (crit =  +0.0°C)  sensor = AMD AMDSI
Thermistor 16:     -40.0°C  (low  =  +0.0°C)
                            (high =  +0.0°C, hyst =  +0.0°C)
                            (crit =  +0.0°C)  sensor = thermistor
pwm1:                  45%
pwm2:                  50%
pwm3:                  45%
pwm4:                 128%
pwm5:                  50%
pwm6:                  50%
pwm7:                  50%
intrusion0:       OK
beep_enable:      disabled

nvme-pci-1100
Adapter: PCI adapter
Composite:    +50.9°C  (low  =  -0.1°C, high = +85.8°C)
                       (crit = +87.8°C)
Sensor 1:     +50.9°C  (low  = -273.1°C, high = +65261.8°C)

nvme-pci-0200
Adapter: PCI adapter
Composite:    +43.9°C  (low  =  -0.1°C, high = +85.8°C)
                       (crit = +87.8°C)
Sensor 1:     +43.9°C  (low  = -273.1°C, high = +65261.8°C)

spd5118-i2c-2-53
Adapter: SMBus PIIX4 adapter port 0 at 0b00
temp1:        +42.5°C  (low  =  +0.0°C, high = +55.0°C)
                       (crit low =  +0.0°C, crit = +85.0°C)

enp9s0-pci-0900
Adapter: PCI adapter
PHY Temperature:  +62.0°C  
MAC Temperature:  +62.0°C  

k10temp-pci-00c3
Adapter: PCI adapter
Tctl:         +48.9°C  
Tccd1:        +39.0°C  
Tccd2:        +39.1°C  

nvme-pci-1400
Adapter: PCI adapter
Composite:    +52.9°C  (low  =  -0.1°C, high = +85.8°C)
                       (crit = +87.8°C)
Sensor 1:     +52.9°C  (low  = -273.1°C, high = +65261.8°C)
```
