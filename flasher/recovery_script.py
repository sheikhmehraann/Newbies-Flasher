import shlex
from typing import List, Tuple


def _q(value: str) -> str:
    return shlex.quote(value)


def _align_4k(size: int) -> int:
    if size <= 0:
        return 4096
    return ((size + 4095) // 4096) * 4096


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

    has_any_images = bool(super_raw or dynamic_specs or bootchain_specs or firmware_specs)
    needs_zstd_bin = bool(use_zstd and has_any_images)
    has_super_stage = bool(super_raw or dynamic_specs)
    has_boot_stage = bool(bootchain_specs)
    has_fw_stage = bool(firmware_specs)
    has_avb_stage = vbmeta_mode in ("disable", "enable")
    needs_unzip = bool(has_any_images or has_avb_stage)
    total_stages = 1 + int(has_super_stage) + int(has_boot_stage) + int(has_fw_stage) + int(has_avb_stage)
    stage_idx = 1

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

    done_bytes = 0

    def next_progress(added_bytes: int) -> str:
        nonlocal done_bytes
        done_bytes += max(0, added_bytes)
        frac = 0.12 + (0.82 * min(1.0, done_bytes / total_flash_bytes))
        return f"{frac:.3f}"

    lines: List[str] = [
        "#!/sbin/sh",
        'if [ -w "/proc/self/fd/$2" ] 2>/dev/null; then',
        '    OUTFD="/proc/self/fd/$2"',
        'elif [ -w "/proc/$$/fd/$2" ] 2>/dev/null; then',
        '    OUTFD="/proc/$$/fd/$2"',
        "else",
        '    OUTFD="/proc/self/fd/$2"',
        "fi",
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
        'UNZIP_BIN=""',
        'UNZIP_BB=""',
        'ZSTD_BIN=""',
        'LPTOOLS=""',
        'AVB_BIN=""',
        'HAS_SUPER=0',
        'SUPER_NAME="super"',
        'SUPER_DEV=""',
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
        '    parent=$(printf \'%s\' "$bname" | sed \'s/[0-9]*$//; s/p$//\')',
        '    if [ -n "$parent" ] && [ "$parent" != "$bname" ] && [ -w "/sys/block/${parent}/force_ro" ]; then',
        '        echo 0 >"/sys/block/${parent}/force_ro" 2>/dev/null || true',
        "    fi",
        "}",
        "",
        "find_block_device() {",
        '    name="$1"',
        "    for base in \\",
        "        /dev/block/by-name \\",
        "        /dev/block/bootdevice/by-name \\",
        "        /dev/block/platform/bootdevice/by-name \\",
        "        /dev/block/platform/*/by-name \\",
        "        /dev/block/platform/*/*/by-name \\",
        "        /dev/block/platform/*/*/*/by-name; do",
        '        if [ -b "$base/$name" ] || [ -L "$base/$name" ]; then',
        '            echo "$base/$name"',
        "            return 0",
        "        fi",
        "    done",
        '    case "$name" in',
        "        preloader|preloader_raw|preloader_a)",
        '            [ -b /dev/block/mmcblk0boot0 ] && { echo "/dev/block/mmcblk0boot0"; return 0; }',
        "            ;;",
        "        preloader_b)",
        '            [ -b /dev/block/mmcblk0boot1 ] && { echo "/dev/block/mmcblk0boot1"; return 0; }',
        "            ;;",
        "    esac",
        "    if command -v find >/dev/null 2>&1; then",
        '        found=$(find /dev/block -maxdepth 5 \\( -type b -o -type l \\) -name "$name" 2>/dev/null | grep -v \'/mapper/\' | head -n 1 || true)',
        '        if [ -n "$found" ]; then',
        '            echo "$found"',
        "            return 0",
        "        fi",
        "    fi",
        "    return 1",
        "}",
        "",
        "ensure_by_name_link() {",
        '    name="$1"; dev="$2"',
        '    [ -n "$dev" ] || return 0',
        '    if [ ! -e "/dev/block/by-name/$name" ]; then',
        "        mkdir -p /dev/block/by-name 2>/dev/null || true",
        '        ln -sf "$dev" "/dev/block/by-name/$name" 2>/dev/null || true',
        "    fi",
        "}",
        "",
    ]

    if needs_unzip:
        lines.extend([
            "resolve_unzip() {",
            '    for cand in "$(command -v unzip 2>/dev/null || true)" /system/bin/unzip /sbin/unzip /vendor/bin/unzip; do',
            '        [ -n "$cand" ] && [ -x "$cand" ] || continue',
            '        if "$cand" -p "$ZIPFILE" "META-INF/com/google/android/update-binary" >/dev/null 2>&1; then',
            '            UNZIP_BIN="$cand"',
            "            return 0",
            "        fi",
            "    done",
            '    for bb in "$(command -v busybox 2>/dev/null || true)" /sbin/busybox /system/bin/busybox; do',
            '        [ -n "$bb" ] && [ -x "$bb" ] || continue',
            '        if "$bb" unzip -p "$ZIPFILE" "META-INF/com/google/android/update-binary" >/dev/null 2>&1; then',
            '            UNZIP_BB="$bb"',
            "            return 0",
            "        fi",
            "    done",
            '    abort "No working unzip binary found in recovery for $ZIPFILE"',
            "}",
            "",
            "run_unzip() {",
            '    if [ -n "$UNZIP_BIN" ]; then',
            '        "$UNZIP_BIN" "$@"',
            "    else",
            '        "$UNZIP_BB" unzip "$@"',
            "    fi",
            "}",
            "",
        ])

    if has_any_images:
        lines.extend([
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
            "flash_raw_to_dev() {",
            '    src="$1"; dest="$2"; raw_sz="$3"; label="$4"',
            '    [ -b "$dest" ] || [ -L "$dest" ] || abort "Target block device not found: $dest"',
            '    unlock_block_rw "$dest"',
            '    check_block_fits "$dest" "$raw_sz" "$label"',
            "    rm -f /tmp/nf_err.log",
            '    if ! run_unzip -p "$ZIPFILE" "$src" >"$dest" 2>/tmp/nf_err.log; then',
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
            '    ( run_unzip -p "$ZIPFILE" "$src" 2>>/tmp/nf_err.log; echo $? >/tmp/nf_unzip_rc ) | "$ZSTD_BIN" -c -d >"$dest" 2>>/tmp/nf_err.log',
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
        ])

    if has_super_stage:
        lines.extend([
            "unmount_dynamic() {",
            "    if [ -r /proc/mounts ]; then",
            "        dm_mounts=$(awk '$1 ~ /^\\/dev\\/block\\/(mapper|dm-)/ {print $2}' /proc/mounts 2>/dev/null | awk '{a[NR]=$0} END{for(i=NR;i>=1;i--) print a[i]}')",
            "        for mnt in $dm_mounts; do",
            '            umount "$mnt" 2>/dev/null || umount -lf "$mnt" 2>/dev/null || true',
            "        done",
            "    fi",
            "    for mp in /system /system_root /mnt/system /vendor /mnt/vendor /product /system_ext \\",
            "              /system_dlkm /vendor_dlkm /odm_dlkm /odm /mi_ext /cust /prism /optics /oem /preload /vgc \\",
            "              /tr_carrier /tr_company /tr_mi /tr_preload /tr_product /tr_region /tr_theme \\",
            "              /tr_manifest /tr_misc /tr_overlayfs /tr_vintf /tr_vendor \\",
            "              /my_product /my_stock /my_heytap /my_carrier /my_region /my_manifest \\",
            "              /my_preload /my_company /my_engineering /my_bigball /my_odm /my_version; do",
            '        umount "$mp" 2>/dev/null || umount -lf "$mp" 2>/dev/null || true',
            "    done",
            "}",
            "",
        ])

    if dynamic_specs:
        lines.extend([
            "detect_super() {",
            '    SUPER_NAME=$(getprop ro.boot.super_partition 2>/dev/null || true)',
            '    [ -n "$SUPER_NAME" ] || SUPER_NAME="super"',
            '    SUPER_DEV=$(find_block_device "$SUPER_NAME" || true)',
            '    if [ -z "$SUPER_DEV" ] && [ "$SUPER_NAME" != "super" ]; then',
            '        SUPER_DEV=$(find_block_device "super" || true)',
            '        [ -n "$SUPER_DEV" ] && SUPER_NAME="super"',
            "    fi",
            '    if [ -n "$SUPER_DEV" ]; then',
            "        HAS_SUPER=1",
            '        ensure_by_name_link "$SUPER_NAME" "$SUPER_DEV"',
            '        ui_print "- Super  : $SUPER_NAME (dynamic)"',
            "    else",
            "        HAS_SUPER=0",
            '        ui_print "- Super  : none (static GPT mode)"',
            "    fi",
            "}",
            "",
            "is_physical_partition() {",
            '    p="$1"',
            '    case "$p" in',
            "        system|vendor|product|system_ext|odm|system_dlkm|vendor_dlkm|odm_dlkm|my_*|tr_*)",
            "            return 1",
            "            ;;",
            "    esac",
            '    cand=$(find_block_device "${p}_a" || find_block_device "${p}_b" || find_block_device "$p" || true)',
            '    [ -n "$cand" ] || return 1',
            '    real=$(readlink -f "$cand" 2>/dev/null || echo "$cand")',
            '    case "$real" in',
            "        /dev/block/dm-*|/dev/dm-*|*/mapper/*) return 1 ;;",
            "    esac",
            "    return 0",
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
            "dm_unmap_force() {",
            '    target="$1"',
            '    "$LPTOOLS" unmap "$target" >/dev/null 2>&1 || true',
            "    if command -v dmctl >/dev/null 2>&1; then",
            '        for sfx in "-cow" "-verity" ""; do',
            '            if [ -e "/dev/block/mapper/${target}${sfx}" ] || [ -e "/dev/mapper/${target}${sfx}" ]; then',
            '                dmctl delete "${target}${sfx}" >/dev/null 2>&1 || true',
            "            fi",
            "        done",
            "    fi",
            "}",
            "",
            "lp_clear_one() {",
            '    p="$1"',
            '    if [ "$IS_AB" -eq 1 ]; then',
            '        dm_unmap_force "${p}_a"',
            '        "$LPTOOLS" remove "${p}_a" >/dev/null 2>&1 || true',
            '        dm_unmap_force "${p}_b"',
            '        "$LPTOOLS" remove "${p}_b" >/dev/null 2>&1 || true',
            "    else",
            '        dm_unmap_force "$p"',
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
        ])

    if has_avb_stage:
        lines.extend([
            "unlock_vbmeta_rw() {",
            "    for vb_name in vbmeta_a vbmeta_b vbmeta; do",
            '        vb_dev=$(find_block_device "$vb_name" || true)',
            '        if [ -n "$vb_dev" ]; then',
            '            ensure_by_name_link "$vb_name" "$vb_dev"',
            '            unlock_block_rw "$vb_dev"',
            "        fi",
            "    done",
            "}",
            "",
            "patch_single_vbmeta_dd() {",
            '    vb_name="$1"; mode="$2"',
            '    if [ "$mode" = "disable" ]; then',
            "        flag_octal='\\000\\000\\000\\003'",
            "    else",
            "        flag_octal='\\000\\000\\000\\000'",
            "    fi",
            '    vb_dev=$(find_block_device "$vb_name" || true)',
            '    [ -n "$vb_dev" ] || return 1',
            '    magic=$(dd if="$vb_dev" bs=4 count=1 2>/dev/null | tr -d \'\\000\')',
            '    if [ "$magic" = "AVB0" ]; then',
            '        unlock_block_rw "$vb_dev"',
            '        if printf "$flag_octal" | dd of="$vb_dev" bs=1 seek=120 count=4 conv=notrunc 2>/dev/null; then',
            '            ui_print "  - Patched $vb_name header directly ($mode)"',
            "            return 0",
            "        fi",
            "    fi",
            "    return 1",
            "}",
            "",
            "patch_vbmeta_block_dd() {",
            '    mode="$1"',
            "    patched=0",
            "    for vb_name in vbmeta_a vbmeta_b vbmeta; do",
            '        if patch_single_vbmeta_dd "$vb_name" "$mode"; then',
            "            patched=$((patched + 1))",
            "        fi",
            "    done",
            '    [ "$patched" -gt 0 ]',
            "}",
            "",
            "apply_avb_mode() {",
            '    mode="$1"',
            '    if [ "$mode" = "disable" ]; then',
            '        state_word="disabled"',
            '        verb="Disabling"',
            "    else",
            '        state_word="enabled"',
            '        verb="Enabling"',
            "    fi",
            "    unlock_vbmeta_rw",
            '    if [ -n "$AVB_BIN" ]; then',
            '        ui_print "- Checking AVB 2.0 vbmeta status"',
            '        V_STATE=$("$AVB_BIN" get-verity 2>&1 || true)',
            '        F_STATE=$("$AVB_BIN" get-verification 2>&1 || true)',
            '        [ -n "$V_STATE" ] && ui_print "  | $V_STATE"',
            '        [ -n "$F_STATE" ] && ui_print "  | $F_STATE"',
            '        if printf \'%s\' "$V_STATE" | grep -qi "$state_word" && printf \'%s\' "$F_STATE" | grep -qi "$state_word"; then',
            '            ui_print "- Active slot vbmeta already $state_word"',
            '            if [ "$IS_AB" -eq 1 ] && [ -n "$OTHER_SLOT" ]; then',
            '                patch_single_vbmeta_dd "vbmeta${OTHER_SLOT}" "$mode" || true',
            "            fi",
            "            return 0",
            "        fi",
            '        ui_print "- $verb AVB 2.0 vbmeta"',
            '        RES_V=$("$AVB_BIN" --force "${mode}-verity" 2>&1)',
            "        RC_V=$?",
            '        RES_F=$("$AVB_BIN" --force "${mode}-verification" 2>&1)',
            "        RC_F=$?",
            '        [ -n "$RES_V" ] && ui_print "  | $RES_V"',
            '        [ -n "$RES_F" ] && ui_print "  | $RES_F"',
            '        if [ "$RC_V" -eq 0 ] && [ "$RC_F" -eq 0 ]; then',
            '            if [ "$IS_AB" -eq 1 ] && [ -n "$OTHER_SLOT" ]; then',
            '                patch_single_vbmeta_dd "vbmeta${OTHER_SLOT}" "$mode" || true',
            "            fi",
            "            return 0",
            "        fi",
            '        ui_print "- avbctl returned error (verity=$RC_V, verification=$RC_F), falling back to direct vbmeta patch"',
            "    else",
            '        ui_print "- avbctl unavailable in recovery, patching vbmeta header directly"',
            "    fi",
            '    if ! patch_vbmeta_block_dd "$mode"; then',
            '        abort "Failed to $mode AVB 2.0 on vbmeta partition"',
            "    fi",
            "}",
            "",
        ])

    lines.extend([
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
        "    actual=$(getprop ro.product.device 2>/dev/null || true)",
        '    [ -n "$actual" ] || actual=$(getprop ro.build.product 2>/dev/null || true)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.vendor.device 2>/dev/null || true)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.system.device 2>/dev/null || true)',
        '    [ -n "$actual" ] || actual=$(getprop ro.product.name 2>/dev/null || true)',
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
        "detect_slot() {",
        "    SLOT=$(getprop ro.boot.slot_suffix 2>/dev/null || true)",
        '    if [ -z "$SLOT" ]; then',
        "        s=$(getprop ro.boot.slot 2>/dev/null || true)",
        '        [ -n "$s" ] && SLOT="_${s#_}"',
        "    fi",
        '    if [ -z "$SLOT" ] && [ -r /proc/cmdline ]; then',
        "        for tok in $(cat /proc/cmdline 2>/dev/null); do",
        '            case "$tok" in',
        "                androidboot.slot_suffix=*)",
        '                    SLOT="${tok#androidboot.slot_suffix=}"',
        "                    break",
        "                    ;;",
        "                androidboot.slot=*)",
        '                    s="${tok#androidboot.slot=}"',
        '                    [ -n "$s" ] && SLOT="_${s#_}"',
        "                    break",
        "                    ;;",
        "            esac",
        "        done",
        "    fi",
        '    case "$SLOT" in',
        '        a|_a) IS_AB=1; SLOT="_a"; OTHER_SLOT="_b"; SLOT_LABEL="Slot A (_a)" ;;',
        '        b|_b) IS_AB=1; SLOT="_b"; OTHER_SLOT="_a"; SLOT_LABEL="Slot B (_b)" ;;',
        '        *)    IS_AB=0; SLOT="";   OTHER_SLOT="";   SLOT_LABEL="A-Only" ;;',
        "    esac",
        "    soc=$(getprop ro.board.platform 2>/dev/null || true)",
        '    [ -n "$soc" ] || soc=$(getprop ro.hardware 2>/dev/null || true)',
        '    [ -n "$soc" ] && ui_print "- SoC    : $soc"',
        '    ui_print "- Slot   : $SLOT_LABEL"',
        "}",
        "",
        "resolve_helpers() {",
    ])

    if needs_unzip:
        lines.append("    resolve_unzip")

    if needs_zstd_bin:
        lines.extend([
            '    run_unzip -o "$ZIPFILE" "META-INF/zstd" -d /tmp >/dev/null 2>&1 || true',
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
            '    if [ "$HAS_SUPER" -eq 1 ]; then',
            '        for cand in /system/bin/lptools /sbin/lptools /vendor/bin/lptools /system/bin/lptools_new /sbin/lptools_new "$(command -v lptools 2>/dev/null || true)"; do',
            '            if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" free >/dev/null 2>&1; then',
            '                LPTOOLS="$cand"',
            "                break",
            "            fi",
            "        done",
            '        if [ -z "$LPTOOLS" ]; then',
            '            run_unzip -o "$ZIPFILE" "META-INF/bin/lptools" -d /tmp >/dev/null 2>&1 || true',
            "            chmod 0755 /tmp/META-INF/bin/lptools 2>/dev/null || true",
            '            if /tmp/META-INF/bin/lptools free >/dev/null 2>&1; then',
            '                LPTOOLS="/tmp/META-INF/bin/lptools"',
            "            fi",
            "        fi",
            '        [ -n "$LPTOOLS" ] || abort "No working lptools binary found in recovery or package"',
            "    fi",
        ])

    if has_avb_stage:
        lines.extend([
            '    for cand in /system/bin/avbctl /sbin/avbctl /vendor/bin/avbctl "$(command -v avbctl 2>/dev/null || true)"; do',
            '        if [ -n "$cand" ] && [ -x "$cand" ]; then',
            '            probe=$("$cand" get-verity 2>&1 || true)',
            '            if printf \'%s\' "$probe" | grep -qi "verity"; then',
            '                AVB_BIN="$cand"',
            "                break",
            "            fi",
            "        fi",
            "    done",
            '    if [ -z "$AVB_BIN" ]; then',
            '        run_unzip -o "$ZIPFILE" "META-INF/bin/avbctl" -d /tmp >/dev/null 2>&1 || true',
            "        chmod 0755 /tmp/META-INF/bin/avbctl 2>/dev/null || true",
            "        if [ -x /tmp/META-INF/bin/avbctl ]; then",
            "            probe=$(/tmp/META-INF/bin/avbctl get-verity 2>&1 || true)",
            '            if printf \'%s\' "$probe" | grep -qi "verity"; then',
            '                AVB_BIN="/tmp/META-INF/bin/avbctl"',
            "            fi",
            "        fi",
            "    fi",
        ])

    if not needs_unzip and not needs_zstd_bin and not dynamic_specs and not has_avb_stage:
        lines.append("    :")

    banner_title = "Newbies Flasher"
    if not has_any_images and has_avb_stage:
        banner_title = f"Newbies Flasher ({'Disable' if vbmeta_mode == 'disable' else 'Enable'} Vbmeta)"

    lines.extend([
        "}",
        "",
        'ui_print "=========================================="',
        f'ui_print "  {banner_title}"',
        'ui_print "=========================================="',
        f'ui_print "Device     : "{dev_q}',
        f'ui_print "Codename   : "{code_display_q}',
        f'ui_print "Version    : "{ver_q}',
        f'ui_print "Maintainer : "{maint_q}',
        f'ui_print "AVB 2.0    : "{_q(vbmeta_mode)}',
        'ui_print "=========================================="',
        'ui_print " "',
        "set_progress 0.04",
        "",
        f'ui_print "[{stage_idx}/{total_stages}] Preparing"',
        'if [ ! -r "$ZIPFILE" ]; then abort "ZIP archive is not readable: $ZIPFILE"; fi',
        "check_device",
        "detect_slot",
    ])
    if dynamic_specs:
        lines.append("detect_super")
    lines.append("resolve_helpers")
    if has_super_stage:
        lines.append("unmount_dynamic")
    lines.extend([
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
            dyn_total = len(dynamic_specs)
            lines.extend([
                'if [ "$HAS_SUPER" -eq 1 ]; then',
                '    cow_out=$("$LPTOOLS" clear-cow 2>&1 || true)',
                '    case "$cow_out" in',
                '        *"merge is pending"*|*"Merge status is NOT none"*)',
                '            ui_print "- Warning: pending Virtual A/B snapshot merge detected"',
                "            command -v snapshotctl >/dev/null 2>&1 && snapshotctl unmap >/dev/null 2>&1 || true",
                "            ;;",
                "    esac",
                "    REQ_SUPER_BYTES=0",
            ])
            for part, raw_sz in dynamic_specs:
                part_q = _q(part)
                alloc_sz = _align_4k(raw_sz)
                lines.append(f"    if ! is_physical_partition {part_q}; then")
                lines.append(f"        lp_clear_one {part_q}")
                lines.append(f"        REQ_SUPER_BYTES=$((REQ_SUPER_BYTES + {alloc_sz}))")
                lines.append("    fi")
            lines.extend([
                '    "$LPTOOLS" unlimited-group >/dev/null 2>&1 || true',
                '    if [ "$REQ_SUPER_BYTES" -gt 0 ]; then',
                '        FREE_BYTES=$("$LPTOOLS" free 2>&1 | awk \'/Free space:/ {print $3; exit}\')',
                '        if [ -n "$FREE_BYTES" ] && [ "$FREE_BYTES" -gt 0 ] 2>/dev/null; then',
                '            ui_print "- Super  : $((FREE_BYTES / 1048576)) MB free / $((REQ_SUPER_BYTES / 1048576)) MB needed"',
                '            if [ "$FREE_BYTES" -lt "$REQ_SUPER_BYTES" ]; then',
                '                abort "Not enough space in $SUPER_NAME (need $((REQ_SUPER_BYTES / 1048576)) MB, have $((FREE_BYTES / 1048576)) MB). Flash stock super.img or cancel pending snapshot merge."',
                "            fi",
                "        fi",
                "    fi",
            ])
            for part, raw_sz in dynamic_specs:
                part_q = _q(part)
                alloc_sz = _align_4k(raw_sz)
                lines.append(f"    if ! is_physical_partition {part_q}; then lp_create_slot {part_q} {alloc_sz}; fi")

            dyn_progress_marks: List[str] = []
            for idx, (part, raw_sz) in enumerate(dynamic_specs, 1):
                part_q = _q(part)
                src_q = _q(part + ext)
                mb = max(1, raw_sz // 1048576)
                prog = next_progress(raw_sz)
                dyn_progress_marks.append(prog)
                lines.append(f"    if is_physical_partition {part_q}; then")
                lines.append(f"        flash_partition_auto {src_q} {part_q} {raw_sz} {comp_flag} '[{idx}/{dyn_total}]'")
                lines.append("    else")
                lines.append(f'        part_slot={part_q}"$SLOT"')
                lines.append('        target_dev=$(find_mapper_device "$part_slot") || abort "Mapper device missing for $part_slot"')
                lines.append(f'        ui_print "  [{idx}/{dyn_total}] $part_slot ({mb} MB)"')
                if use_zstd:
                    lines.append(f'        flash_zstd_to_dev {src_q} "$target_dev" {raw_sz} "$part_slot"')
                else:
                    lines.append(f'        flash_raw_to_dev {src_q} "$target_dev" {raw_sz} "$part_slot"')
                lines.append("    fi")
                lines.append(f"    set_progress {prog}")

            lines.append("    sync")
            for part, _ in dynamic_specs:
                part_q = _q(part)
                lines.append(f'    if ! is_physical_partition {part_q}; then')
                lines.append(f'        "$LPTOOLS" unmap {part_q}"$SLOT" >/dev/null 2>&1 || true')
                lines.append(f'        "$LPTOOLS" map {part_q}"$SLOT" >/dev/null 2>&1 || true')
                lines.append("    fi")

            lines.append("else")
            lines.append('    ui_print "- Flashing system/vendor images directly to static GPT partitions"')
            for idx, (part, raw_sz) in enumerate(dynamic_specs, 1):
                lines.append(f"    flash_partition_auto {_q(part + ext)} {_q(part)} {raw_sz} {comp_flag} '[{idx}/{dyn_total}]'")
                lines.append(f"    set_progress {dyn_progress_marks[idx - 1]}")
            lines.append("fi")
        lines.append('ui_print " "')

    if has_boot_stage:
        stage_idx += 1
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] Bootchain partitions"')
        boot_total = len(bootchain_specs)
        for idx, (part, raw_sz) in enumerate(bootchain_specs, 1):
            lines.append(f"flash_partition_auto {_q(part + ext)} {_q(part)} {raw_sz} {comp_flag} '[{idx}/{boot_total}]'")
            lines.append(f"set_progress {next_progress(raw_sz)}")
        lines.append('ui_print " "')

    if has_fw_stage:
        stage_idx += 1
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] Firmware partitions"')
        fw_total = len(firmware_specs)
        for idx, (fw, raw_sz) in enumerate(firmware_specs, 1):
            lines.append(f"flash_partition_auto {_q(fw + ext)} {_q(fw)} {raw_sz} {comp_flag} '[{idx}/{fw_total}]'")
            lines.append(f"set_progress {next_progress(raw_sz)}")
        lines.append('ui_print " "')

    if has_avb_stage:
        stage_idx += 1
        label = "Disable" if vbmeta_mode == "disable" else "Enable"
        lines.append(f'ui_print "[{stage_idx}/{total_stages}] AVB 2.0 Vbmeta ({label})"')
        lines.append(f'apply_avb_mode "{vbmeta_mode}"')
        lines.append("set_progress 0.96")
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
        'ui_print "Format Data if stuck or switching ROMs/AVB state."',
        'ui_print "=========================================="',
        "exit 0",
        "",
    ])
    return "\n".join(lines)
