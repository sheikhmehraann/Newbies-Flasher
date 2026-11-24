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
    bootchain_specs: List[Tuple[str, int]],
    firmware_specs: List[Tuple[str, int]],
    vbmeta_mode: str = "skip",
    use_zstd: bool = True,
    verify_sha256: bool = True,
    clean_super: bool = True,
) -> str:
    ext = ".img.zst" if use_zstd else ".img"
    dev_q = _q(device)
    ver_q = _q(version)
    code_q = _q(codename)
    maint_q = _q(maintainer)
    total_dyn_bytes = sum(sz for _, sz in dynamic_specs)

    lines: List[str] = [
        "#!/sbin/sh",
        'OUTFD="/proc/self/fd/$2"',
        'ZIPFILE="$3"',
        "",
        "ui_print() {",
        '    printf \'ui_print %s\\nui_print\\n\' "$1" >>"$OUTFD"',
        "}",
        "",
        "set_progress() {",
        '    printf \'set_progress %s\\n\' "$1" >>"$OUTFD"',
        "}",
        "",
        "abort() {",
        '    ui_print " "',
        '    ui_print "ERROR: $1"',
        '    ui_print "Installation aborted."',
        "    exit 1",
        "}",
        "",
        'export PATH="/sbin:/system/bin:/vendor/bin:/bin:/tmp/META-INF/bin:/tmp/META-INF:$PATH"',
        'ZSTD_BIN=""',
        'LPTOOLS=""',
        'AVB_BIN=""',
        "",
        "cleanup() {",
        "    rm -f /tmp/nf_unzip_rc /tmp/nf_err.log /tmp/nf_sha256sums.txt 2>/dev/null || true",
        "}",
        "trap cleanup EXIT",
        "",
        "print_err_tail() {",
        "    if [ -s /tmp/nf_err.log ]; then",
        "        while IFS= read -r eline; do",
        '            [ -n "$eline" ] && ui_print "  | $eline"',
        "        done </tmp/nf_err.log",
        "    fi",
        "}",
        "",
        "unlock_block_rw() {",
        '    dev="$1"',
        '    command -v blockdev >/dev/null 2>&1 && blockdev --setrw "$dev" >/dev/null 2>&1 || true',
        '    real=$(readlink -f "$dev" 2>/dev/null || echo "$dev")',
        '    bname=$(basename "$real")',
        '    [ -w "/sys/block/${bname}/force_ro" ] && echo 0 >"/sys/block/${bname}/force_ro" 2>/dev/null || true',
        '    parent=$(printf \'%s\' "$bname" | sed \'s/p\\?[0-9]*$//\')',
        '    [ -n "$parent" ] && [ -w "/sys/block/${parent}/force_ro" ] && echo 0 >"/sys/block/${parent}/force_ro" 2>/dev/null || true',
        "}",
        "",
        "check_block_fits() {",
        '    dest="$1"; need="$2"; label="$3"',
        '    [ "$need" -gt 0 ] 2>/dev/null || return 0',
        "    command -v blockdev >/dev/null 2>&1 || return 0",
        '    cap=$(blockdev --getsize64 "$dest" 2>/dev/null || echo 0)',
        '    if [ -n "$cap" ] && [ "$cap" -gt 0 ] 2>/dev/null && [ "$need" -gt "$cap" ]; then',
        '        abort "$label image ($need bytes) is larger than target partition ($cap bytes)"',
        "    fi",
        "}",
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
        '    src="$1"; dest="$2"; raw_sz="$3"; label="$4"',
        '    [ -b "$dest" ] || [ -L "$dest" ] || abort "Target block device not found: $dest"',
        '    unlock_block_rw "$dest"',
        '    check_block_fits "$dest" "$raw_sz" "$label"',
        "    rm -f /tmp/nf_err.log",
        '    if ! unzip -p "$ZIPFILE" "$src" >"$dest" 2>/tmp/nf_err.log; then',
        "        print_err_tail",
        '        abort "Failed flashing $src to $dest"',
        "    fi",
        "}",
        "",
        "flash_zstd_to_dev() {",
        '    src="$1"; dest="$2"; raw_sz="$3"; label="$4"',
        '    [ -b "$dest" ] || [ -L "$dest" ] || abort "Target block device not found: $dest"',
        '    unlock_block_rw "$dest"',
        '    check_block_fits "$dest" "$raw_sz" "$label"',
        "    rm -f /tmp/nf_unzip_rc /tmp/nf_err.log",
        '    ( unzip -p "$ZIPFILE" "$src" 2>>/tmp/nf_err.log; echo $? >/tmp/nf_unzip_rc ) | "$ZSTD_BIN" -c -d >"$dest" 2>>/tmp/nf_err.log',
        "    z_rc=$?",
        "    u_rc=$(cat /tmp/nf_unzip_rc 2>/dev/null || echo 1)",
        '    if [ "$u_rc" -ne 0 ]; then',
        "        print_err_tail",
        '        abort "ZIP extraction failed for $src (exit $u_rc). Re-download archive or check storage."',
        "    fi",
        '    if [ "$z_rc" -ne 0 ]; then',
        "        print_err_tail",
        '        abort "ZSTD decode or write failed for $src -> $dest (exit $z_rc)"',
        "    fi",
        "}",
        "",
        "flash_partition_auto() {",
        '    src="$1"; part="$2"; raw_sz="$3"; compressed="$4"',
        '    dev_a=$(find_block_device "${part}_a" || true)',
        '    dev_b=$(find_block_device "${part}_b" || true)',
        '    if [ -n "$dev_a" ] || [ -n "$dev_b" ]; then',
        '        ui_print "  [OK] Flashing $part (slot A + B)"',
        '        if [ -n "$dev_a" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_a" "$raw_sz" "${part}_a"; else flash_raw_to_dev "$src" "$dev_a" "$raw_sz" "${part}_a"; fi',
        "        fi",
        '        if [ -n "$dev_b" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_b" "$raw_sz" "${part}_b"; else flash_raw_to_dev "$src" "$dev_b" "$raw_sz" "${part}_b"; fi',
        "        fi",
        "    else",
        '        dev_single=$(find_block_device "$part" || true)',
        '        [ -n "$dev_single" ] || abort "Block device not found for partition: $part"',
        '        ui_print "  [OK] Flashing $part"',
        '        if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_single" "$raw_sz" "$part"; else flash_raw_to_dev "$src" "$dev_single" "$raw_sz" "$part"; fi',
        "    fi",
        "}",
        "",
        "verify_manifest() {",
        "    if ! command -v sha256sum >/dev/null 2>&1; then",
        '        ui_print "  - sha256sum unavailable in recovery, skipping hash check"',
        "        return 0",
        "    fi",
        '    unzip -p "$ZIPFILE" "META-INF/sha256sums.txt" >/tmp/nf_sha256sums.txt 2>/dev/null || return 0',
        '    ui_print "- Verifying package checksums..."',
        "    checked=0",
        "    while IFS= read -r line; do",
        '        [ -n "$line" ] || continue',
        '        expected=$(printf \'%s\' "$line" | awk \'{print $1}\')',
        '        entry=$(printf \'%s\' "$line" | awk \'{print $2}\')',
        '        [ -n "$expected" ] && [ -n "$entry" ] || continue',
        '        actual=$(unzip -p "$ZIPFILE" "$entry" | sha256sum | awk \'{print $1}\')',
        '        [ "$actual" = "$expected" ] || abort "Corrupt archive: SHA-256 mismatch on $entry"',
        "        checked=$((checked + 1))",
        "    done </tmp/nf_sha256sums.txt",
        '    ui_print "  -> Verified $checked file(s)"',
        "}",
        "",
        "get_volume_key() {",
        '    ui_print "- Press Volume Up to continue, Volume Down to abort"',
        "    while true; do",
        "        ev=$(getevent -qlc 1 2>/dev/null | grep KEY_VOLUME || true)",
        '        [ -n "$ev" ] || continue',
        "        if printf '%s\\n' \"$ev\" | grep -q KEY_VOLUMEUP; then return 0; fi",
        "        if printf '%s\\n' \"$ev\" | grep -q KEY_VOLUMEDOWN; then return 1; fi",
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
        "    actual_lc=$(printf '%s' \"$actual\" | tr 'A-Z' 'a-z')",
        "    expected_lc=$(printf '%s' \"$expected\" | tr 'A-Z' 'a-z')",
        '    if [ "$actual_lc" != "$expected_lc" ]; then',
        '        ui_print "- Device mismatch: phone is $actual, package is for $expected"',
        '        if ! get_volume_key; then abort "Cancelled due to device mismatch"; fi',
        "    else",
        '        ui_print "- Device verified : $actual"',
        "    fi",
        "}",
        "",
        "unmount_dynamic() {",
        "    # Unmount any live mountpoints backed by dm/mapper devices first (fixes Unable to unmap).",
        "    if [ -r /proc/mounts ]; then",
        "        dm_mounts=$(awk '$1 ~ /^\\/dev\\/block\\/(mapper|dm-)/ {print $2}' /proc/mounts 2>/dev/null | awk '{a[NR]=$0} END{for(i=NR;i>=1;i--) print a[i]}')",
        "        for mnt in $dm_mounts; do",
        '            umount "$mnt" 2>/dev/null || umount -lf "$mnt" 2>/dev/null || true',
        "        done",
        "    fi",
        "    for mp in /system /system_root /vendor /product /system_ext /vendor_dlkm /odm_dlkm /odm \\",
        "              /tr_carrier /tr_company /tr_mi /tr_preload /tr_product /tr_region /tr_theme \\",
        "              /tr_manifest /tr_misc /my_product /my_stock /my_heytap /my_carrier /my_region \\",
        "              /my_manifest /my_preload /my_company /my_engineering /my_bigball /cust /prism /optics; do",
        '        umount "$mp" 2>/dev/null || umount -lf "$mp" 2>/dev/null || true',
        "    done",
        "}",
        "",
        "detect_slot() {",
        "    SLOT=$(getprop ro.boot.slot_suffix)",
        '    if [ -z "$SLOT" ]; then',
        "        s=$(getprop ro.boot.slot)",
        '        [ -n "$s" ] && SLOT="_${s}"',
        "    fi",
        '    if [ "$SLOT" = "_a" ]; then',
        '        IS_AB=1; OTHER_SLOT="_b"; SLOT_LABEL="Slot A (_a)"',
        '    elif [ "$SLOT" = "_b" ]; then',
        '        IS_AB=1; OTHER_SLOT="_a"; SLOT_LABEL="Slot B (_b)"',
        "    else",
        '        IS_AB=0; SLOT=""; OTHER_SLOT=""; SLOT_LABEL="A-Only"',
        "    fi",
        '    ui_print "- Active slot     : $SLOT_LABEL"',
        "}",
        "",
        "lp_clear_one() {",
        '    p="$1"',
        '    if [ "$IS_AB" -eq 1 ]; then',
        '        "$LPTOOLS" unmap "${p}_a" >/dev/null 2>&1 || true',
        '        "$LPTOOLS" remove "${p}_a" >/dev/null 2>&1 || true',
        '        "$LPTOOLS" unmap "${p}_b" >/dev/null 2>&1 || true',
        '        "$LPTOOLS" remove "${p}_b" >/dev/null 2>&1 || true',
        "    else",
        '        "$LPTOOLS" unmap "$p" >/dev/null 2>&1 || true',
        '        "$LPTOOLS" remove "$p" >/dev/null 2>&1 || true',
        "    fi",
        "}",
        "",
        "lp_clear_all_stale() {",
        "    if [ -d /dev/block/mapper ]; then",
        "        for node in /dev/block/mapper/*; do",
        '            [ -e "$node" ] || continue',
        '            nm=$(basename "$node")',
        '            case "$nm" in',
        "                control|by-uuid|userdata|metadata|cache|persist|misc|frp|nvdata|nvram|protect*|proinfo|seccfg|*-verity*) continue ;;",
        "            esac",
        '            "$LPTOOLS" unmap "$nm" >/dev/null 2>&1 || true',
        '            "$LPTOOLS" remove "$nm" >/dev/null 2>&1 || true',
        "        done",
        "    fi",
        "}",
        "",
        "lp_create_slot() {",
        '    p="$1"; sz="$2"',
        "    rm -f /tmp/nf_err.log",
        '    if ! "$LPTOOLS" create "${p}${SLOT}" "$sz" >/tmp/nf_err.log 2>&1; then',
        "        print_err_tail",
        '        abort "Failed to create logical partition ${p}${SLOT} ($((sz / 1048576)) MB)"',
        "    fi",
        '    if [ -n "$OTHER_SLOT" ]; then',
        '        "$LPTOOLS" create "${p}${OTHER_SLOT}" 0 >/dev/null 2>&1 || true',
        "    fi",
        "}",
        "",
        "resolve_helpers() {",
    ]

    if use_zstd:
        lines.extend([
            '    unzip -o "$ZIPFILE" "META-INF/zstd" -d /tmp >/dev/null 2>&1 || true',
            "    chmod 0755 /tmp/META-INF/zstd 2>/dev/null || true",
            '    if /tmp/META-INF/zstd --version >/dev/null 2>&1; then',
            '        ZSTD_BIN="/tmp/META-INF/zstd"',
            "    elif command -v zstd >/dev/null 2>&1 && zstd --version >/dev/null 2>&1; then",
            '        ZSTD_BIN="$(command -v zstd)"',
            "    else",
            '        abort "No working zstd binary available in package or recovery"',
            "    fi",
        ])

    if dynamic_specs:
        lines.extend([
            "    # Prefer recovery's native lptools first (matches Android 14-16 liblp/AIDL), fallback to bundled.",
            "    for cand in /system/bin/lptools /sbin/lptools /vendor/bin/lptools; do",
            '        if [ -x "$cand" ] && "$cand" free >/dev/null 2>&1; then',
            '            LPTOOLS="$cand"',
            "            break",
            "        fi",
            "    done",
            '    if [ -z "$LPTOOLS" ]; then',
            '        unzip -o "$ZIPFILE" "META-INF/bin/lptools" -d /tmp >/dev/null 2>&1 || true',
            "        chmod 0755 /tmp/META-INF/bin/lptools 2>/dev/null || true",
            '        if /tmp/META-INF/bin/lptools free >/dev/null 2>&1; then',
            '            LPTOOLS="/tmp/META-INF/bin/lptools"',
            "        fi",
            "    fi",
            '    [ -n "$LPTOOLS" ] || abort "No working lptools binary found in recovery or package"',
        ])

    if vbmeta_mode in ("disable", "enable"):
        lines.extend([
            "    for cand in /system/bin/avbctl /sbin/avbctl; do",
            '        if [ -x "$cand" ] && "$cand" get-verity >/dev/null 2>&1; then',
            '            AVB_BIN="$cand"',
            "            break",
            "        fi",
            "    done",
            '    if [ -z "$AVB_BIN" ]; then',
            '        unzip -o "$ZIPFILE" "META-INF/bin/avbctl" -d /tmp >/dev/null 2>&1 || true',
            "        chmod 0755 /tmp/META-INF/bin/avbctl 2>/dev/null || true",
            '        if /tmp/META-INF/bin/avbctl get-verity >/dev/null 2>&1; then',
            '            AVB_BIN="/tmp/META-INF/bin/avbctl"',
            "        fi",
            "    fi",
        ])

    lines.extend([
        "}",
        "",
        'ui_print "=========================================="',
        'ui_print "             Newbies Flasher"',
        'ui_print "=========================================="',
        f'ui_print "Device     : "{dev_q}',
        f'ui_print "Codename   : "{code_q}',
        f'ui_print "Version    : "{ver_q}',
        f'ui_print "Maintainer : "{maint_q}',
        'ui_print "=========================================="',
        'ui_print " "',
        "set_progress 0.05",
        "",
        "ui_print \"[1/4] Pre-flight checks\"",
        'if [ ! -f "$ZIPFILE" ]; then abort "ZIP archive path is not readable: $ZIPFILE"; fi',
        "check_device",
        "detect_slot",
        "resolve_helpers",
    ])

    if verify_sha256:
        lines.append("verify_manifest")

    lines.extend([
        "unmount_dynamic",
        "set_progress 0.15",
        'ui_print " "',
    ])

    comp_flag = "1" if use_zstd else "0"

    # Stage 2: Super / dynamic partitions FIRST (before touching bootchain or firmware).
    if super_raw or dynamic_specs:
        lines.append('ui_print "[2/4] Dynamic / Super partitions"')
        if super_raw:
            lines.append(f"flash_partition_auto {_q('super' + ext)} 'super' 0 {comp_flag}")
        if dynamic_specs:
            lines.append('"$LPTOOLS" clear-cow >/dev/null 2>&1 || true')
            if clean_super:
                lines.append("lp_clear_all_stale")
            for part, _ in dynamic_specs:
                lines.append(f"lp_clear_one {_q(part)}")
            # Lift group maximum_size cap (prevents ENOSPC when flashing larger/different ROM layouts).
            lines.append('"$LPTOOLS" unlimited-group >/dev/null 2>&1 || true')
            lines.extend([
                f"REQ_SUPER_BYTES={total_dyn_bytes}",
                'FREE_BYTES=$("$LPTOOLS" free 2>/dev/null | awk \'/Free space:/ {print $3; exit}\')',
                'if [ -n "$FREE_BYTES" ] && [ "$FREE_BYTES" -gt 0 ] 2>/dev/null; then',
                '    ui_print "  -> Super free: $((FREE_BYTES / 1048576)) MB | Required: $((REQ_SUPER_BYTES / 1048576)) MB"',
                '    if [ "$FREE_BYTES" -lt "$REQ_SUPER_BYTES" ]; then',
                '        abort "Not enough space in super (need $((REQ_SUPER_BYTES / 1048576)) MB, have $((FREE_BYTES / 1048576)) MB). Flash stock super.img or free space first."',
                "    fi",
                "fi",
            ])
            for part, raw_sz in dynamic_specs:
                lines.append(f"lp_create_slot {_q(part)} {raw_sz}")
            lines.append("set_progress 0.25")
            for part, raw_sz in dynamic_specs:
                src_q = _q(part + ext)
                mb = max(1, raw_sz // 1048576)
                lines.append(f'target_dev=$(find_mapper_device "{part}${{SLOT}}") || abort "Mapper device missing for {part}${{SLOT}}"')
                lines.append(f'ui_print "  [OK] Flashing {part}${{SLOT}} ({mb} MB)"')
                if use_zstd:
                    lines.append(f'flash_zstd_to_dev {src_q} "$target_dev" {raw_sz} "{part}${{SLOT}}"')
                else:
                    lines.append(f'flash_raw_to_dev {src_q} "$target_dev" {raw_sz} "{part}${{SLOT}}"')
            lines.append("sync")
            for part, _ in dynamic_specs:
                lines.append(f'"$LPTOOLS" unmap "{part}${{SLOT}}" >/dev/null 2>&1 || true')
                lines.append(f'"$LPTOOLS" map "{part}${{SLOT}}" >/dev/null 2>&1 || true')
        lines.append("set_progress 0.70")
        lines.append('ui_print " "')

    # Stage 3: Bootchain & AVB 2.0
    if bootchain_specs or vbmeta_mode in ("disable", "enable"):
        lines.append('ui_print "[3/4] Bootchain & AVB 2.0"')
        for part, raw_sz in bootchain_specs:
            lines.append(f"flash_partition_auto {_q(part + ext)} {_q(part)} {raw_sz} {comp_flag}")

        if vbmeta_mode in ("disable", "enable"):
            action = "disable" if vbmeta_mode == "disable" else "enable"
            state_word = "disabled" if vbmeta_mode == "disable" else "enabled"
            lines.extend([
                'if [ -n "$AVB_BIN" ]; then',
                '    V_STATE=$("$AVB_BIN" get-verity 2>/dev/null || true)',
                '    F_STATE=$("$AVB_BIN" get-verification 2>/dev/null || true)',
                f'    if printf \'%s\' "$V_STATE" | grep -qi "{state_word}" && printf \'%s\' "$F_STATE" | grep -qi "{state_word}"; then',
                f'        ui_print "  -> AVB 2.0 already {state_word}"',
                "    else",
                f'        "$AVB_BIN" --force {action}-verity >/dev/null 2>&1 || true',
                f'        "$AVB_BIN" --force {action}-verification >/dev/null 2>&1 || true',
                f'        ui_print "  -> AVB 2.0 set to {state_word}"',
                "    fi",
                "else",
                '    ui_print "  - avbctl unavailable in recovery; relying on packaged vbmeta"',
                "fi",
            ])
        lines.append("set_progress 0.85")
        lines.append('ui_print " "')

    # Stage 4: Firmware & Bootloader (last, only after super & bootchain succeeded)
    if firmware_specs:
        lines.append('ui_print "[4/4] Firmware partitions"')
        for fw, raw_sz in firmware_specs:
            lines.append(f"flash_partition_auto {_q(fw + ext)} {_q(fw)} {raw_sz} {comp_flag}")
        lines.append('ui_print " "')

    lines.extend([
        "sync",
        "set_progress 1.0",
        'ui_print "=========================================="',
        'ui_print "         Installation Complete"',
        'ui_print "=========================================="',
        'ui_print "Note: If switching ROMs or Android versions,"',
        'ui_print "format Data in recovery before rebooting."',
        "exit 0",
        "",
    ])
    return "\n".join(lines)
