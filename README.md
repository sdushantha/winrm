# winrm

An _opinionated_ fork of [`evil-winrm-py`](https://github.com/adityatelange/evil-winrm-py) stripped down and modified for a better user experience and to follow the Unix philosophy; do one thing and do it well. This tool gets you a WinRM shell and it does it quite well.

https://github.com/user-attachments/assets/63ec9630-7e12-4ab5-a5fa-f2cd3fa09054

## Installation

```console
$ uv tool install git+https://github.com/sdushantha/winrm
```

## Improvements Overview

### `winrm` as the command

Having `evil-winrm` and `evil-winrm-py` as the package names is understandable, however using them when the `winrm` command already do not exist, decreases your workflow speed unnecessarily.

### Host as a positional argument

Inspired by [`netexec`](https://github.com/Pennyw0rth/NetExec), the host is now positional argument instead of an `-i`/`--ip` flag. Since most pentesters already use `netexec`, this makes the transition `netexec` to `winrm` seamless.

See in the demo above how quickly we can go from checking the credentials to obtaining a shell!

### Removed Download/Upload
File transfer speeds was unstable for me in `evil-winrm`, but I'm unsure how it is on `evil-winrm-py`. Additionally, `evil-winrm-py` advices you to "use absolute paths for upload/download for reliability" and this is also an issue I have experienced in `evil-winrm`. I have therefore always transfered files through a SMB share with the help of `smbserver.py` from [Impacket](https://github.com/fortra/impacket).

### Removed ability to load things in memory to bypass AV
Using `Invoke-WebRequest` and `Invoke-Expression` should do the job.

### Removed branding
ASCII art banners and branding in the prompt may be cool, but I consider it a waste of my terminal emulator's real estate.
