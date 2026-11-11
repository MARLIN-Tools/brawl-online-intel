import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
ELECTRON_URL = "https://github.com/electron/electron/releases/download/v{v}/electron-v{v}-darwin-x64.zip"
ELECTRON_RELEASES = "https://releases.electronjs.org/releases.json"
SQLITE_URL = ("https://github.com/WiseLibs/better-sqlite3/releases/download/v{v}/"
              "better-sqlite3-v{v}-electron-v{abi}-darwin-x64.tar.gz")
HELPER_SUFFIXES = ["", " (GPU)", " (Plugin)", " (Renderer)"]
SHARED_FRAMEWORKS = ["Electron Framework", "Mantle", "ReactiveObjC", "Squirrel"]
CPU_X86_64 = 0x01000007


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "brawl-online-intel"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def fetch_json(url):
    import json
    req = urllib.request.Request(url, headers={"User-Agent": "brawl-online-intel"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def read_plist(path):
    with open(path, "rb") as f:
        return plistlib.load(f)


def fuse_wire(data):
    i = data.find(FUSE_SENTINEL)
    if i < 0:
        return None
    start = i + len(FUSE_SENTINEL)
    version, length = data[start], data[start + 1]
    return start, version, length


def copy_fuses(src_binary, dst_binary):
    src = open(src_binary, "rb").read()
    dst = bytearray(open(dst_binary, "rb").read())
    s, d = fuse_wire(src), fuse_wire(dst)
    if s is None or d is None:
        return "no fuse wire found, defaults kept"
    if s[1:] != d[1:]:
        return "fuse wire layouts differ (%s vs %s), defaults kept" % (s[1:], d[1:])
    n = s[2]
    wire = src[s[0] + 2:s[0] + 2 + n]
    dst[d[0] + 2:d[0] + 2 + n] = wire
    open(dst_binary, "wb").write(dst)
    return "fuses copied: %s" % wire.decode("ascii", "replace")


def macho_archs(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    if len(head) < 8:
        return None
    magic_be = struct.unpack_from(">I", head, 0)[0]
    if magic_be in (0xCAFEBABE, 0xCAFEBABF):
        n = struct.unpack_from(">I", head, 4)[0]
        step = 20 if magic_be == 0xCAFEBABE else 32
        return {struct.unpack_from(">i", head, 8 + step * k)[0] for k in range(n)}
    if struct.unpack_from("<I", head, 0)[0] == 0xFEEDFACF:
        return {struct.unpack_from("<i", head, 4)[0]}
    return None


def non_intel_binaries(app):
    bad, count = [], 0
    for root, dirs, files in os.walk(app):
        for name in files:
            p = os.path.join(root, name)
            if os.path.islink(p):
                continue
            archs = macho_archs(p)
            if archs is None:
                continue
            count += 1
            if CPU_X86_64 not in archs:
                bad.append(os.path.relpath(p, app))
    return count, bad


def replace_tree(src, dst):
    if os.path.lexists(dst):
        shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
    subprocess.run(["ditto", src, dst], check=True)


def swap_electron(app, work):
    contents = os.path.join(app, "Contents")
    frameworks = os.path.join(contents, "Frameworks")
    info = read_plist(os.path.join(frameworks, "Electron Framework.framework", "Resources", "Info.plist"))
    version = info["CFBundleVersion"]
    exe_name = read_plist(os.path.join(contents, "Info.plist"))["CFBundleExecutable"]
    print("Electron %s, app executable %r" % (version, exe_name))

    zip_path = fetch(ELECTRON_URL.format(v=version), os.path.join(work, "electron-x64.zip"))
    x64 = os.path.join(work, "electron-x64")
    shutil.rmtree(x64, ignore_errors=True)
    subprocess.run(["ditto", "-x", "-k", zip_path, x64], check=True)
    x64_app = os.path.join(x64, "Electron.app", "Contents")

    old_framework_binary = os.path.join(work, "electron-framework-arm64")
    shutil.copy2(os.path.join(frameworks, "Electron Framework.framework", "Versions", "A", "Electron Framework"),
                 old_framework_binary)

    shutil.copy2(os.path.join(x64_app, "MacOS", "Electron"), os.path.join(contents, "MacOS", exe_name))
    for fw in SHARED_FRAMEWORKS:
        src = os.path.join(x64_app, "Frameworks", fw + ".framework")
        if os.path.isdir(src):
            replace_tree(src, os.path.join(frameworks, fw + ".framework"))
    print(copy_fuses(old_framework_binary,
                     os.path.join(frameworks, "Electron Framework.framework", "Versions", "A", "Electron Framework")))

    for suffix in HELPER_SUFFIXES:
        mine = os.path.join(frameworks, "%s Helper%s.app" % (exe_name, suffix))
        theirs = os.path.join(x64_app, "Frameworks", "Electron Helper%s.app" % suffix)
        if not os.path.isdir(mine):
            continue
        helper_exe = read_plist(os.path.join(mine, "Contents", "Info.plist"))["CFBundleExecutable"]
        shutil.copy2(os.path.join(theirs, "Contents", "MacOS", "Electron Helper%s" % suffix),
                     os.path.join(mine, "Contents", "MacOS", helper_exe))
    return version


def swap_sqlite(app, work, electron_version):
    unpacked = os.path.join(app, "Contents", "Resources", "app.asar.unpacked", "node_modules", "better-sqlite3")
    if not os.path.isdir(unpacked):
        print("no better-sqlite3 in this version")
        return
    import json
    version = json.load(open(os.path.join(unpacked, "package.json")))["version"]
    abi = next(r["modules"] for r in fetch_json(ELECTRON_RELEASES) if r["version"] == electron_version)
    tgz = fetch(SQLITE_URL.format(v=version, abi=abi), os.path.join(work, "better-sqlite3-x64.tar.gz"))
    with tarfile.open(tgz) as t:
        member = t.getmember("build/Release/better_sqlite3.node")
        data = t.extractfile(member).read()
    dest = os.path.join(unpacked, "build", "Release", "better_sqlite3.node")
    with open(dest, "wb") as f:
        f.write(data)
    os.chmod(dest, 0o755)
    print("better-sqlite3 %s for Electron ABI %s" % (version, abi))


def swap_dolphin(app, dolphin_dir):
    dest = os.path.join(app, "Contents", "Resources", "dolphin")
    replace_tree(dolphin_dir, dest)
    print("Dolphin replaced from %s" % dolphin_dir)


def sign(app, entitlements):
    frameworks = os.path.join(app, "Contents", "Frameworks")
    nested = []
    for root, dirs, files in os.walk(frameworks):
        for name in files:
            p = os.path.join(root, name)
            if not os.path.islink(p) and macho_archs(p) is not None:
                nested.append(p)
    for p in sorted(nested, key=lambda q: -q.count(os.sep)):
        subprocess.run(["codesign", "--force", "--sign", "-", p], check=True)
    for name in sorted(os.listdir(frameworks)):
        if name.endswith((".framework", ".app")):
            subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements", entitlements,
                            os.path.join(frameworks, name)], check=True)
    for root, dirs, files in os.walk(os.path.join(app, "Contents", "Resources", "app.asar.unpacked")):
        for name in files:
            if name.endswith(".node"):
                subprocess.run(["codesign", "--force", "--sign", "-", os.path.join(root, name)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements", entitlements, app], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)


def main():
    app, dolphin_dir, entitlements, work = sys.argv[1:5]
    os.makedirs(work, exist_ok=True)
    electron_version = swap_electron(app, work)
    swap_sqlite(app, work, electron_version)
    swap_dolphin(app, dolphin_dir)
    count, bad = non_intel_binaries(app)
    print("%d programs and libraries checked" % count)
    if bad:
        for b in bad:
            print("::error::no Intel code in %s" % b)
        sys.exit(1)
    sign(app, entitlements)
    print("signed")


if __name__ == "__main__":
    main()
