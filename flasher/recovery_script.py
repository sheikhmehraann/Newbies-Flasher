import shlex
from typing import List, Tuple


def _q(value: str) -> str:
    return shlex.quote(value)


def generate_update_binary(
    device: str,
    version: str,
    codename: str,
    maintainer: str,
    dynamic_specs: List[Tuple[str, int]],
    super_raw: bool,
    bootchain_imgs: List[str],
    firmware_imgs: List[str],
    vbmeta_mode: str = "skip",
    use_zstd: bool = True,
    verify_sha256: bool = True,
) -> str:
    ext = ".img.zst" if use_zstd else ".img"
    dev_q = _q(device)
    ver_q = _q(version)
    code_q = _q(codename)
    maint_q = _q(maintainer)

    lines: List[str] = [
        "#!/sbin/sh",
        'OUTFD="/proc/self/fd/$2"',
        'ZIPFILE="$3"',
        "",
        "ui_print() {",
        '    printf \'ui_print %s\\nui_print\\n\' "$1" >>"$OUTFD"',
        "}",
        "",
        "abort() {",
        '    ui_print "ERROR: $1"',
        "    exit 1",
        "}",
        "",
        'export PATH="/tmp/META-INF/bin:/tmp/META-INF:/sbin:/system/bin:/bin:$PATH"',
        'LPTOOLS="/tmp/META-INF/bin/lptools"',
        '[ -x "$LPTOOLS" ] || LPTOOLS="lptools"',
        'ZSTD_BIN="/tmp/META-INF/zstd"',
        '[ -x "$ZSTD_BIN" ] || ZSTD_BIN="zstd"',
        'AVB_BIN="/tmp/META-INF/bin/avbctl"',
        '[ -x "$AVB_BIN" ] || AVB_BIN="avbctl"',
        "",
        "cleanup() {",
        "    rm -f /tmp/nf_unzip_rc /tmp/nf_sha256sums.txt 2>/dev/null || true",
        "}",
        "trap cleanup EXIT",
        "",
        "find_block_device() {",
        '    name="$1"',
        "    for base in /dev/block/by-name /dev/block/bootdevice/by-name /dev/block/platform/*/by-name /dev/block/platform/*/*/by-name; do",
        '        if [ -b "$base/$name" ] || [ -L "$base/$name" ]; then',
        '            echo "$base/$name"',
        "            return 0",
        "        fi",
        "    done",
        "    return 1",
        "}",
        "",
        "find_mapper_device() {",
        '    name="$1"',
        "    for base in /dev/block/mapper /dev/mapper /dev/block/by-name; do",
        '        if [ -b "$base/$name" ] || [ -L "$base/$name" ]; then',
        '            echo "$base/$name"',
        "            return 0",
        "        fi",
        "    done",
        '    "$LPTOOLS" map "$name" >/dev/null 2>&1 || true',
        "    for base in /dev/block/mapper /dev/mapper /dev/block/by-name; do",
        '        if [ -b "$base/$name" ] || [ -L "$base/$name" ]; then',
        '            echo "$base/$name"',
        "            return 0",
        "        fi",
        "    done",
        "    return 1",
        "}",
        "",
        "flash_raw_to_dev() {",
        '    src="$1"; dest="$2"',
        '    [ -b "$dest" ] || [ -L "$dest" ] || abort "Target block device not found: $dest"',
        '    unzip -p "$ZIPFILE" "$src" >"$dest" || abort "Failed flashing $src to $dest"',
        "}",
        "",
        "flash_zstd_to_dev() {",
        '    src="$1"; dest="$2"',
        '    [ -b "$dest" ] || [ -L "$dest" ] || abort "Target block device not found: $dest"',
        "    rm -f /tmp/nf_unzip_rc",
        '    ( unzip -p "$ZIPFILE" "$src"; echo $? >/tmp/nf_unzip_rc ) | "$ZSTD_BIN" -c -d >"$dest"',
        "    z_rc=$?",
        "    u_rc=$(cat /tmp/nf_unzip_rc 2>/dev/null || echo 1)",
        '    [ "$u_rc" -eq 0 ] || abort "ZIP extraction failed for $src (exit $u_rc)"',
        '    [ "$z_rc" -eq 0 ] || abort "ZSTD decode or write failed for $src -> $dest (exit $z_rc)"',
        "}",
        "",
        "flash_partition_auto() {",
        '    src="$1"; part="$2"; compressed="$3"',
        '    dev_a=$(find_block_device "${part}_a" || true)',
        '    dev_b=$(find_block_device "${part}_b" || true)',
        '    if [ -n "$dev_a" ] || [ -n "$dev_b" ]; then',
        '        ui_print "  -> $part (A/B)"',
        '        if [ -n "$dev_a" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_a"; else flash_raw_to_dev "$src" "$dev_a"; fi',
        "        fi",
        '        if [ -n "$dev_b" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_b"; else flash_raw_to_dev "$src" "$dev_b"; fi',
        "        fi",
        "    else",
        '        dev_single=$(find_block_device "$part" || true)',
        '        [ -n "$dev_single" ] || abort "Block device not found for partition: $part"',
        '        ui_print "  -> $part"',
        '        if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_single"; else flash_raw_to_dev "$src" "$dev_single"; fi',
        "    fi",
        "}",
        "",
        "verify_zip_entry() {",
        '    entry="$1"; expected="$2"',
        "    command -v sha256sum >/dev/null 2>&1 || return 0",
        '    actual=$(unzip -p "$ZIPFILE" "$entry" | sha256sum | awk \'{print $1}\')',
        '    [ "$actual" = "$expected" ] || abort "Checksum mismatch for $entry (archive may be corrupt)"',
        "}",
        "",
        "verify_manifest() {",
        "    command -v sha256sum >/dev/null 2>&1 || return 0",
        '    unzip -p "$ZIPFILE" "META-INF/sha256sums.txt" >/tmp/nf_sha256sums.txt 2>/dev/null || return 0',
        '    ui_print "- Verifying package integrity..."',
        "    while IFS= read -r line; do",
        '        [ -n "$line" ] || continue',
        '        expected=$(printf \'%s\' "$line" | awk \'{print $1}\')',
        '        entry=$(printf \'%s\' "$line" | awk \'{print $2}\')',
        '        [ -n "$expected" ] && [ -n "$entry" ] && verify_zip_entry "$entry" "$expected"',
        "    done </tmp/nf_sha256sums.txt",
        '    ui_print "  -> Checksums OK"',
        "}",
        "",
        "get_volume_key() {",
        '    ui_print "- Press Volume Up to continue, Volume Down to abort"',
        "    while true; do",
        "        ev=$(getevent -qlc 1 2>/dev/null | grep KEY_VOLUME || true)",
        '        [ -n "$ev" ] || continue',
        '        if printf \'%s\\n\' "$ev" | grep -q KEY_VOLUMEUP; then return 0; fi',
        '        if printf \'%s\\n\' "$ev" | grep -q KEY_VOLUMEDOWN; then return 1; fi',
        "    done",
        "}",
        "",
        "check_device() {",
        f"    expected={code_q}",
        '    [ -n "$expected" ] || return 0',
        "    actual=$(getprop ro.product.device)",
        '    [ -n "$actual" ] || actual=$(getprop ro.build.product)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.name)',
        '    [ -n "$actual" ] || return 0',
        '    actual_lc=$(printf \'%s\' "$actual" | tr \'A-Z\' \'a-z\')',
        '    expected_lc=$(printf \'%s\' "$expected" | tr \'A-Z\' \'a-z\')',
        '    if [ "$actual_lc" != "$expected_lc" ]; then',
        '        ui_print "- Device mismatch: found $actual, expected $expected"',
        "        if ! get_volume_key; then abort \"Installation cancelled due to device mismatch\"; fi",
        "    else",
        '        ui_print "- Device verified: $actual"',
        "    fi",
        "}",
        "",
        "unmount_dynamic() {",
        "    umount -f -l /system /system_root /vendor /product /system_ext /vendor_dlkm /odm_dlkm /odm \\",
        "                 /tr_carrier /tr_company /tr_mi /tr_preload /tr_product /tr_region /tr_theme \\",
        "                 /tr_manifest /tr_misc /my_product /my_stock /my_heytap /my_carrier /my_region \\",
        "                 /my_manifest /my_preload /my_company /my_engineering /my_bigball /cust /prism /optics 2>/dev/null || true",
        "}",
        "",
        "detect_slot() {",
        "    SLOT=$(getprop ro.boot.slot_suffix)",
        '    if [ -z "$SLOT" ]; then',
        "        s=$(getprop ro.boot.slot)",
        '        [ -n "$s" ] && SLOT="_${s}"',
        "    fi",
        '    if [ "$SLOT" = "_a" ]; then',
        '        IS_AB=1; OTHER_SLOT="_b"; SLOT_LABEL="A"',
        '    elif [ "$SLOT" = "_b" ]; then',
        '        IS_AB=1; OTHER_SLOT="_a"; SLOT_LABEL="B"',
        "    else",
        '        IS_AB=0; SLOT=""; OTHER_SLOT=""; SLOT_LABEL="A-Only"',
        "    fi",
        '    ui_print "- Active Slot: $SLOT_LABEL"',
        "}",
        "",
        "lp_clear() {",
        '    p="$1"',
        '    if [ "$IS_AB" -eq 1 ]; then',
        '        "$LPTOOLS" unmap "${p}_a" 2>/dev/null || true',
        '        "$LPTOOLS" remove "${p}_a" 2>/dev/null || true',
        '        "$LPTOOLS" unmap "${p}_b" 2>/dev/null || true',
        '        "$LPTOOLS" remove "${p}_b" 2>/dev/null || true',
        "    else",
        '        "$LPTOOLS" unmap "$p" 2>/dev/null || true',
        '        "$LPTOOLS" remove "$p" 2>/dev/null || true',
        "    fi",
        "}",
        "",
        "lp_create_slot() {",
        '    p="$1"; sz="$2"',
        '    "$LPTOOLS" create "${p}${SLOT}" "$sz" || abort "lptools create failed for ${p}${SLOT} ($sz bytes)"',
        '    if [ -n "$OTHER_SLOT" ]; then',
        '        "$LPTOOLS" create "${p}${OTHER_SLOT}" 0 2>/dev/null || true',
        "    fi",
        "}",
        "",
        "extract_helpers() {",
    ]

    if use_zstd:
        lines.extend([
            '    unzip -o "$ZIPFILE" "META-INF/zstd" -d /tmp >/dev/null 2>&1 || true',
            "    chmod 0755 /tmp/META-INF/zstd 2>/dev/null || true",
            '    [ -x /tmp/META-INF/zstd ] && ZSTD_BIN="/tmp/META-INF/zstd"',
        ])
    if dynamic_specs:
        lines.extend([
            '    unzip -o "$ZIPFILE" "META-INF/bin/lptools" -d /tmp >/dev/null 2>&1 || true',
            "    chmod 0755 /tmp/META-INF/bin/lptools 2>/dev/null || true",
            '    [ -x /tmp/META-INF/bin/lptools ] && LPTOOLS="/tmp/META-INF/bin/lptools"',
            '    "$LPTOOLS" free >/dev/null 2>&1 || LPTOOLS="lptools"',
        ])
    if vbmeta_mode in ("disable", "enable"):
        lines.extend([
            '    unzip -o "$ZIPFILE" "META-INF/bin/avbctl" -d /tmp >/dev/null 2>&1 || true',
            "    chmod 0755 /tmp/META-INF/bin/avbctl 2>/dev/null || true",
            '    [ -x /tmp/META-INF/bin/avbctl ] && AVB_BIN="/tmp/META-INF/bin/avbctl"',
            '    "$AVB_BIN" get-verity >/dev/null 2>&1 || AVB_BIN="avbctl"',
        ])

    lines.extend([
        "}",
        "",
        "extract_helpers",
        "",
        'ui_print "=========================================="',
        'ui_print "             Newbies Flasher"',
        'ui_print "=========================================="',
        f'ui_print "Device     : "{dev_q}',
        f'ui_print "Codename   : "{code_q}',
        f'ui_print "Version    : "{ver_q}',
        f'ui_print "Maintainer : "{maint_q}',
        'ui_print "=========================================="',
        "",
        "check_device",
        "unmount_dynamic",
        "detect_slot",
    ])

    if verify_sha256:
        lines.append("verify_manifest")

    if dynamic_specs:
        lines.append('"$LPTOOLS" clear-cow >/dev/null 2>&1 || true')
    lines.append("")

    comp_flag = "1" if use_zstd else "0"

    if firmware_imgs:
        lines.append('ui_print "- Flashing firmware..."')
        for fw in firmware_imgs:
            lines.append(f"flash_partition_auto {_q(fw + ext)} {_q(fw)} {comp_flag}")
        lines.append("")

    if bootchain_imgs:
        lines.append('ui_print "- Flashing bootchain..."')
        for part in bootchain_imgs:
            lines.append(f"flash_partition_auto {_q(part + ext)} {_q(part)} {comp_flag}")
        lines.append("")

    if vbmeta_mode in ("disable", "enable"):
        action = "disable" if vbmeta_mode == "disable" else "enable"
        state_word = "disabled" if vbmeta_mode == "disable" else "enabled"
        lines.extend([
            f'ui_print "- Setting AVB 2.0 state ({action})..."',
            'if command -v "$AVB_BIN" >/dev/null 2>&1 || [ -x "$AVB_BIN" ]; then',
            '    V_STATE=$("$AVB_BIN" get-verity 2>/dev/null || true)',
            '    F_STATE=$("$AVB_BIN" get-verification 2>/dev/null || true)',
            f'    if printf \'%s\' "$V_STATE" | grep -qi "{state_word}" && printf \'%s\' "$F_STATE" | grep -qi "{state_word}"; then',
            f'        ui_print "  -> AVB already {state_word}"',
            "    else",
            f'        "$AVB_BIN" --force {action}-verity >/dev/null 2>&1 || true',
            f'        "$AVB_BIN" --force {action}-verification >/dev/null 2>&1 || true',
            f'        ui_print "  -> AVB set to {state_word}"',
            "    fi",
            "fi",
            "",
        ])

    if super_raw:
        lines.append('ui_print "- Flashing super image..."')
        lines.append(f"flash_partition_auto {_q('super' + ext)} 'super' {comp_flag}")
        lines.append("")

    if dynamic_specs:
        lines.append('ui_print "- Flashing dynamic partitions..."')
        for part, _ in dynamic_specs:
            lines.append(f"lp_clear {_q(part)}")
        for part, raw_sz in dynamic_specs:
            lines.append(f"lp_create_slot {_q(part)} {raw_sz}")
        for part, _ in dynamic_specs:
            src_q = _q(part + ext)
            lines.append(f'target_dev=$(find_mapper_device "{part}${{SLOT}}") || abort "Mapper device missing for {part}${{SLOT}}"')
            lines.append(f'ui_print "  -> {part}${{SLOT}}"')
            if use_zstd:
                lines.append(f'flash_zstd_to_dev {src_q} "$target_dev"')
            else:
                lines.append(f'flash_raw_to_dev {src_q} "$target_dev"')
        lines.append("sync")
        for part, _ in dynamic_specs:
            lines.append(f'"$LPTOOLS" unmap "{part}${{SLOT}}" >/dev/null 2>&1 || true')
            lines.append(f'"$LPTOOLS" map "{part}${{SLOT}}" >/dev/null 2>&1 || true')
        lines.append("")

    lines.extend([
        "sync",
        'ui_print "=========================================="',
        'ui_print "         Installation Complete"',
        'ui_print "=========================================="',
        "exit 0",
        "",
    ])
    return "\n".join(lines)
