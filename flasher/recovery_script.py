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
    super_raw_size: int = 0,
) -> str:
    ext = ".img.zst" if use_zstd else ".img"
    dev_q = _q(device)
    ver_q = _q(version)
    code_q = _q(codename)
    code_display_q = _q(codename if codename else "(any)")
    maint_q = _q(maintainer)

    super_weight = super_raw_size if (super_raw and super_raw_size > 0) else (1 if super_raw else 0)
    total_dyn_bytes = sum(sz for _, sz in dynamic_specs)
    total_flash_bytes = (
        total_dyn_bytes
        + sum(sz for _, sz in bootchain_specs)
        + sum(sz for _, sz in firmware_specs)
        + super_weight
    )
    if total_flash_bytes <= 0:
        total_flash_bytes = 1

    has_super_stage = bool(super_raw or dynamic_specs)
    has_boot_stage = bool(bootchain_specs or vbmeta_mode in ("disable", "enable"))
    has_fw_stage = bool(firmware_specs)
    total_stages = 1 + int(has_super_stage) + int(has_boot_stage) + int(has_fw_stage)
    stage_idx = 1

    done_bytes = 0

    def next_progress(added_bytes: int) -> str:
        nonlocal done_bytes
        done_bytes += max(0, added_bytes)
        frac = 0.12 + (0.86 * min(1.0, done_bytes / total_flash_bytes))
        return f"{frac:.3f}"

    lines: List[str] = [
        "#!/sbin/sh",
        'OUTFD="/proc/self/fd/$2"',
        'ZIPFILE="$3"',
        "START_TS=$(date +%s 2>/dev/null || echo 0)",
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
        "    rm -f /tmp/nf_unzip_rc /tmp/nf_err.log 2>/dev/null || true",
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
        '        abort "$label image ($need bytes) exceeds target partition ($cap bytes)"',
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
        '        abort "ZIP extraction failed for $src (exit $u_rc). Re-download ZIP or check storage."',
        "    fi",
        '    if [ "$z_rc" -ne 0 ]; then',
        "        print_err_tail",
        '        abort "ZSTD decode/write failed for $src -> $dest (exit $z_rc). Archive may be corrupt."',
        "    fi",
        "}",
        "",
        "flash_partition_auto() {",
        '    src="$1"; part="$2"; raw_sz="$3"; compressed="$4"; tag="$5"',
        '    dev_a=$(find_block_device "${part}_a" || true)',
        '    dev_b=$(find_block_device "${part}_b" || true)',
        '    if [ -n "$dev_a" ] || [ -n "$dev_b" ]; then',
        '        ui_print "  $tag $part (slot A + B)"',
        '        if [ -n "$dev_a" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_a" "$raw_sz" "${part}_a"; else flash_raw_to_dev "$src" "$dev_a" "$raw_sz" "${part}_a"; fi',
        "        fi",
        '        if [ -n "$dev_b" ]; then',
        '            if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_b" "$raw_sz" "${part}_b"; else flash_raw_to_dev "$src" "$dev_b" "$raw_sz" "${part}_b"; fi',
        "        fi",
        "    else",
        '        dev_single=$(find_block_device "$part" || true)',
        '        [ -n "$dev_single" ] || abort "Block device not found for partition: $part"',
        '        ui_print "  $tag $part"',
        '        if [ "$compressed" = "1" ]; then flash_zstd_to_dev "$src" "$dev_single" "$raw_sz" "$part"; else flash_raw_to_dev "$src" "$dev_single" "$raw_sz" "$part"; fi',
        "    fi",
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
        f"    expected_list={code_q}",
        '    [ -n "$expected_list" ] || return 0',
        "    actual=$(getprop ro.product.device)",
        '    [ -n "$actual" ] || actual=$(getprop ro.build.product)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.vendor.device)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.system.device)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.name)',
        '    [ -n "$actual" ] || return 0',
        "    actual_lc=$(printf '%s' \"$actual\" | tr 'A-Z' 'a-z')",
        "    matched=0",
        "    old_ifs=$IFS",
        "    IFS=','",
        "    for item in $expected_list; do",
        "        cand=$(printf '%s' \"$item\" | tr -d ' ' | tr 'A-Z' 'a-z')",
        '        [ -n "$cand" ] || continue',
        "        # Accept exact match or OEM prefix like Infinix-X6871 matching X6871.",
        '        case "$actual_lc" in',
        '            "$cand"|*-"$cand"|*_"$cand") matched=1; break ;;',
        "        esac",
        '        case "$cand" in',
        '            *-"$actual_lc"|*_"$actual_lc") matched=1; break ;;',
        "        esac",
        "    done",
        "    IFS=$old_ifs",
        '    if [ "$matched" -eq 1 ]; then',
        '        ui_print "- Device : $actual (verified)"',
        "    else",
        '        ui_print "- Device mismatch: phone is $actual, package expects $expected_list"',
        '        if ! get_volume_key; then abort "Cancelled due to device mismatch"; fi',
        "    fi",
        "}",
        "",
        "unmount_dynamic() {",
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
        '    ui_print "- Slot   : $SLOT_LABEL"',
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
        f'ui_print "Codename   : "{code_display_q}',
        f'ui_print "Version    : "{ver_q}',
        f'ui_print "Maintainer : "{maint_q}',
        'ui_print "=========================================="',
        'ui_print " "',
        "set_progress 0.04",
        "",
        f'ui_print "[{stage_idx}/{total_stages}] Preparing"',
        'if [ ! -f "$ZIPFILE" ]; then abort "ZIP archive is not readable: $ZIPFILE"; fi',
        "check_device",
        "detect_slot",
        "resolve_helpers",
        "unmount_dynamic",
        "set_progress 0.12",
        'ui_print " "',
    ])

    comp_flag = "1" if use_zstd else "0"

    if has_super_stage:
        stage_idx += 1
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] Dynamic / Super partitions"')
        if super_raw:
            lines.append(f"flash_partition_auto {_q('super' + ext)} 'super' {super_raw_size} {comp_flag} '[1/1]'")
            lines.append(f"set_progress {next_progress(super_weight)}")
        if dynamic_specs:
            lines.extend([
                'super_dev=$(find_block_device "super" || true)',
                '[ -n "$super_dev" ] || abort "Device does not have a super partition"',
                '"$LPTOOLS" clear-cow >/dev/null 2>&1 || true',
            ])
            for part, _ in dynamic_specs:
                lines.append(f"lp_clear_one {_q(part)}")
            lines.append('"$LPTOOLS" unlimited-group >/dev/null 2>&1 || true')
            lines.extend([
                f"REQ_SUPER_BYTES={total_dyn_bytes}",
                'FREE_BYTES=$("$LPTOOLS" free 2>&1 | awk \'/Free space:/ {print $3; exit}\')',
                'if [ -n "$FREE_BYTES" ] && [ "$FREE_BYTES" -gt 0 ] 2>/dev/null; then',
                '    ui_print "- Super  : $((FREE_BYTES / 1048576)) MB free / $((REQ_SUPER_BYTES / 1048576)) MB needed"',
                '    if [ "$FREE_BYTES" -lt "$REQ_SUPER_BYTES" ]; then',
                '        abort "Not enough space in super (need $((REQ_SUPER_BYTES / 1048576)) MB, have $((FREE_BYTES / 1048576)) MB). Flash stock super.img first."',
                "    fi",
                "fi",
            ])
            for part, raw_sz in dynamic_specs:
                lines.append(f"lp_create_slot {_q(part)} {raw_sz}")
            dyn_total = len(dynamic_specs)
            for idx, (part, raw_sz) in enumerate(dynamic_specs, 1):
                part_q = _q(part)
                src_q = _q(part + ext)
                mb = max(1, raw_sz // 1048576)
                lines.append(f'part_slot={part_q}"$SLOT"')
                lines.append('target_dev=$(find_mapper_device "$part_slot") || abort "Mapper device missing for $part_slot"')
                lines.append(f'ui_print "  [{idx}/{dyn_total}] $part_slot ({mb} MB)"')
                if use_zstd:
                    lines.append(f'flash_zstd_to_dev {src_q} "$target_dev" {raw_sz} "$part_slot"')
                else:
                    lines.append(f'flash_raw_to_dev {src_q} "$target_dev" {raw_sz} "$part_slot"')
                lines.append(f"set_progress {next_progress(raw_sz)}")
            lines.append("sync")
            for part, _ in dynamic_specs:
                part_q = _q(part)
                lines.append(f'"$LPTOOLS" unmap {part_q}"$SLOT" >/dev/null 2>&1 || true')
                lines.append(f'"$LPTOOLS" map {part_q}"$SLOT" >/dev/null 2>&1 || true')
        lines.append('ui_print " "')

    if has_boot_stage:
        stage_idx += 1
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] Bootchain & AVB 2.0"')
        boot_total = len(bootchain_specs)
        for idx, (part, raw_sz) in enumerate(bootchain_specs, 1):
            lines.append(f"flash_partition_auto {_q(part + ext)} {_q(part)} {raw_sz} {comp_flag} '[{idx}/{boot_total}]'")
            lines.append(f"set_progress {next_progress(raw_sz)}")

        if vbmeta_mode in ("disable", "enable"):
            action = "disable" if vbmeta_mode == "disable" else "enable"
            state_word = "disabled" if vbmeta_mode == "disable" else "enabled"
            lines.extend([
                'if [ -n "$AVB_BIN" ]; then',
                '    V_STATE=$("$AVB_BIN" get-verity 2>/dev/null || true)',
                '    F_STATE=$("$AVB_BIN" get-verification 2>/dev/null || true)',
                f'    if printf \'%s\' "$V_STATE" | grep -qi "{state_word}" && printf \'%s\' "$F_STATE" | grep -qi "{state_word}"; then',
                f'        ui_print "  - AVB 2.0 : already {state_word}"',
                "    else",
                f'        "$AVB_BIN" --force {action}-verity >/dev/null 2>&1 || true',
                f'        "$AVB_BIN" --force {action}-verification >/dev/null 2>&1 || true',
                f'        ui_print "  - AVB 2.0 : {state_word}"',
                "    fi",
                "else",
                '    ui_print "  - AVB 2.0 : avbctl unavailable, using packaged vbmeta"',
                "fi",
            ])
        lines.append('ui_print " "')

    if has_fw_stage:
        stage_idx += 1
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] Firmware partitions"')
        fw_total = len(firmware_specs)
        for idx, (fw, raw_sz) in enumerate(firmware_specs, 1):
            lines.append(f"flash_partition_auto {_q(fw + ext)} {_q(fw)} {raw_sz} {comp_flag} '[{idx}/{fw_total}]'")
            lines.append(f"set_progress {next_progress(raw_sz)}")
        lines.append('ui_print " "')

    lines.extend([
        "sync",
        "set_progress 1.0",
        "END_TS=$(date +%s 2>/dev/null || echo 0)",
        'ELAPSED=""',
        'if [ "$START_TS" -gt 0 ] 2>/dev/null && [ "$END_TS" -ge "$START_TS" ] 2>/dev/null; then',
        "    DIFF=$((END_TS - START_TS))",
        '    if [ "$DIFF" -ge 60 ]; then',
        '        ELAPSED=" ($((DIFF / 60))m $((DIFF % 60))s)"',
        "    else",
        '        ELAPSED=" (${DIFF}s)"',
        "    fi",
        "fi",
        'ui_print "=========================================="',
        'ui_print "Installation complete${ELAPSED}"',
        'ui_print "Format Data if switching ROMs or Android versions."',
        'ui_print "=========================================="',
        "exit 0",
        "",
    ])
    return "\n".join(lines)
