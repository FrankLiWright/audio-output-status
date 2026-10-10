# Windows 音频输出状态

一个小工具，实时显示 Windows **当前播放设备**的实际输出参数与电平、频谱。

![UI 说明] 窗口采用浅色「资源管理器」风格，打开即用、几乎无交互。

## 功能

| 区块 | 内容 |
|------|------|
| **输出设备** | 当前默认播放设备名称 |
| **实际输出格式（实时）** | 编码、采样率、位深、声道、码率、帧字节 — 来自 WASAPI mix format（即你此刻听到的引擎输出） |
| **电平 / 频谱** | 绿·黄·红 LED 电平条（含峰值保持）+ 对数频率频谱（20 Hz–20 kHz） |
| **音频会话** | 全部音频会话可滚动列表（进程名、PID、活跃状态、系统声音） |

## 截图 / 界面要点

- 电平条：声卡式分段 LED，绿 → 黄 → 红，附峰值白线与 dB 读数
- 频谱：DAW 风格，对数频率刻度（20 / 50 / 100 / … / 20k），响度以条高表示
- 格式区：标签定宽对齐，约 250 ms 刷新

## 说明：关于「每个会话的格式」

Windows **不会**向其他进程公开各音频会话的源采样率 / 位深。共享模式下所有应用都会被重采样到设备 mix format，因此本工具显示的是：

> **你人耳听到的设备输出格式**（而非某播放器内部的解码格式）

会话列表可查看哪些进程在播放，但无法显示其各自的源格式。

## 运行

### 方式一：直接运行脚本

```powershell
cd audio-status
pip install -r requirements.txt
python audio_status.py
```

文本快照：

```powershell
python audio_status.py --text
```

### 方式二：打包为 EXE

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name "音频输出状态" `
  --collect-all comtypes --collect-all pycaw --collect-all numpy `
  audio_status.py
```

生成物位于 `dist\音频输出状态.exe`。

## 依赖

- Windows 10 / 11
- Python 3.10+（开发时使用 3.14）
- [pycaw](https://github.com/AndreMiras/pycaw) / comtypes / numpy / psutil

## 实现要点

- **WASAPI loopback** 采集当前输出，用于电平与频谱
- **FFT**（Hann 窗，2048 点）→ 对数频带响度
- **IAudioMeterInformation** 作为电平兜底
- 每线程 `CoInitialize`，采集跑在后台线程
- Per-Monitor DPI，避免文字发虚

## License

[MIT](./LICENSE)
