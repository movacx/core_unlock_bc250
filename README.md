# bc250-core-unlock — desbloqueo **permanente** de núcleos CPU (sin flashear BIOS)

Desbloquea los **2 núcleos CPU ocultos** de la AMD BC-250 (6c/12t → **8c/16t**) y
lo mantiene en **cada arranque en frío**, sin tocar la BIOS ni el arranque UEFI.

> El descubrimiento y la primitiva de bajo nivel son de
> **[rw-r-r-0644/bc250-core-unlock](https://github.com/rw-r-r-0644/bc250-core-unlock)** (MIT).
> Esta herramienta solo lo **automatiza con una guarda de seguridad**. Ver [PROVENANCE.md](PROVENANCE.md).

---

## Por qué "permanente" aquí significa "reaplicar en cada arranque"

La máscara de presencia de núcleos vive en el SMN `0x5A870` y de fábrica lee
`0x77` (6 núcleos). El host **no puede** escribir ese registro; solo el SMU,
mediante el mensaje `0x98`. Al escribir `0xFF`, AGESA enumera los 8 núcleos en
el **siguiente** arranque.

Ese cambio es **volátil**: sobrevive a un reinicio en caliente, pero un apagado
completo lo revierte a `0x77`. **No existe** forma de fijarlo en hardware sin una
de estas dos cosas:

| Método | Permanencia | Riesgo | Esta herramienta |
|---|---|---|---|
| **Flashear BIOS** (mod DXE de RescueMei) | Total, instantáneo | Alto (brickeo) | ❌ No |
| **Shim EFI** (Hexxeh) | Sí, pre-OS | Reescribe orden de arranque UEFI → riesgo de brickeo | ❌ No |
| **Reaplicar al arrancar (este proyecto)** | Sí, automático | **Muy bajo**: solo OS, reversible, sin firmware | ✅ **Sí** |

Por eso esta herramienta instala un pequeño **servicio de arranque** que escribe
`0xFF` muy temprano y reinicia **una vez**, de modo que el sistema levanta con 8
núcleos. Es 100 % a nivel de sistema operativo:

- **Nunca** toca firmware UEFI, la ESP, Secure Boot ni el orden de arranque →
  **no puede dejar la placa sin arrancar.**
- Va **antes** de los governors de GPU (no los desactiva).
- Lleva una **guarda anti-bucle de reinicios**: como máximo **2** reinicios
  automáticos; si la placa no conserva la máscara, se **rinde y se desactiva
  sola**, y arrancas normal con 6 núcleos. Nada de bucles infinitos.
- Se puede desactivar desde el gestor de arranque con `bc250.core_unlock=0`, y
  quitar en cualquier momento con `uninstall`.

## Uso

```bash
# Ver estado (solo lectura; no toca el hardware)
sudo ./bc250-core-unlock status

# Leer además la máscara real (una lectura bajo candado; no hace falta parar nada)
sudo ./bc250-core-unlock status --read-mask

# Ensayo: qué decidiría, sin guardar, escribir ni reiniciar (governor detenido)
sudo ./bc250-core-unlock apply --dry-run

# Instalar el servicio de arranque (NO escribe nada todavía)
sudo ./bc250-core-unlock install

# ...o instalar y desbloquear ya (requiere el governor detenido; reinicia una vez)
sudo systemctl stop cyan-skillfish-governor-smu
sudo ./bc250-core-unlock install --apply-now

# Quitar el servicio (el próximo arranque en frío vuelve a 6 núcleos)
sudo ./bc250-core-unlock uninstall
```

Tras `install`, en el **siguiente arranque** que encuentre la máscara de fábrica
(el próximo reinicio y, después, cada arranque en frío) la placa desbloquea los
dos núcleos y reinicia una vez automáticamente; luego levanta con 8c/16t. Si ya
está desbloqueada, el servicio lo detecta y no hace nada (cero reinicios extra).

El servicio ejecuta `apply --boot`: solo en ese modo desatendido rige el tope de
2 reinicios y la auto-desactivación. Un `apply` lanzado a mano no gasta ese tope
(quien lo lanza es quien corta el bucle).

## Comportamiento de la guarda (`decide_action`)

| Máscara | Hilos *present* | Acción |
|---|---|---|
| `0xFF` | ≥16 | Ya desbloqueado → nada (reinicia el contador) |
| `0x77` | <16 | Escribe `0xFF` y reinicia (1 intento) |
| `0xFF` | <16 | Reinicio pendiente → reinicia una vez |
| cualquiera | — | Tras 2 intentos sin éxito → **se rinde y se desactiva** |
| otra (ej. `0xB7`) | — | **Rechaza** por defecto (posible harvest de fábrica); `--force-mask` para forzar |

Se cuenta *present* (`/sys/devices/system/cpu/present`, de la MADT), no *online*,
para no confundir un `nosmt` con un fallo. La instalación **rechaza** si hay
`nr_cpus=` o `maxcpus=` en la línea de comandos del kernel (falsearían el conteo).

El intento se guarda en disco (con `fsync`) **antes** de escribir al SMU: si no
se puede guardar, no se escribe ni se reinicia.

Todo esto está cubierto por pruebas **sin hardware** (decisión pura, y el flujo
completo contra un SMU simulado, incluidos los candados):

```bash
python3 -I tests/test_guard_logic.py
```

```bash
python3 -I tests/test_apply_flow.py
```

## Avisos importantes

- **Los 2 núcleos nuevos no están validados.** Pueden haber sido desactivados por
  un motivo. Haz estrés (`stress-ng`, `mprime`) unas horas y revisa `dmesg` por
  MCEs antes de confiar en ellos.
- **Cada arranque en frío** añade un reinicio parcial (~10–20 s) y, con LUKS,
  tendrás que **teclear la passphrase dos veces**. Es inherente a que la máscara
  es volátil (el shim EFI también reinicia dos veces, solo que antes del OS).
- **Reporte de reloj GPU tras el desbloqueo:** `pp_dpm_sclk` / `freq1_input`
  reportan valores sin sentido (~18–60 MHz); los relojes reales están bien. Ya
  ocurre con el desbloqueo temporal. Qué implica para el governor cyan:
  - Con `set-method = "smu"` (el valor por defecto de cyan) el governor **no usa**
    esos archivos para decidir la frecuencia: fija y lee el reloj por el SMU.
  - Con `set-method = "kernel"` sí lee `pp_dpm_sclk` para acotar la frecuencia
    (`kernel_freq_strategy.rs`), así que ahí un valor basura **sí** podría
    afectar. Si usas ese modo, compruébalo antes de dejarlo permanente.
  - cyan trae `fix-freq`, que sustituye `freq1_input` por el valor leído del SMU.

  Hexxeh lo corrige en origen con parches a la SRAM del SMU, que requieren el
  "unlock seguro" complejo y quedan **fuera de alcance** aquí (ver PROVENANCE.md).

- **Convive con las demás herramientas del SMU.** La ventana SMN (PCI config
  `0xB8`/`0xBC`) es compartida y no atómica: la usan el governor cyan, el lector
  de temperatura GDDR6 y el ajuste de CPU del Control Center. Esta herramienta
  toma los mismos candados que ellos (`/run/bc250-smu-run.lock` y `flock` sobre
  el archivo de configuración PCI) y deja el registro índice `0xB8` como lo
  encontró.
  - **Leer** la máscara (`status --read-mask`) es una sola lectura bajo candado:
    se puede hacer con el governor en marcha.
  - **Escribir** (`apply`) usa el buzón del SMU, así que **rechaza** si hay un
    governor activo. En el arranque la unidad va *antes* de los governors y el
    buzón está libre. A mano: `sudo systemctl stop cyan-skillfish-governor-smu`.

- **Suspensión e hibernación: sin probar.** La máscara vive mientras el SoC tiene
  energía. No se sabe qué pasa con los 2 núcleos extra al volver de S3/S4.
- Requiere **systemd** u **OpenRC**. Rutas de instalación bajo `/etc` y `/var`,
  compatibles con distros inmutables (SteamOS, Bazzite).

## Integración futura en BC-250 Control Center

El Control Center ya tiene el desbloqueo **temporal** y un detector de
persistencia (`core_unlock_persistence.py`) que reconoce el shim EFI pero se
niega a instalarlo por el riesgo de brickeo. Este mecanismo "boot-service" es el
candidato seguro para ofrecer persistencia desde la app: habría que añadir un
mecanismo nuevo (p. ej. `"boot-service"`) junto a `efi-shim` / `volatile` /
`stock`. Eso es un paso aparte.

## Licencia

MIT. El crédito del descubrimiento es de rw-r-r-0644. Ver [LICENSE](LICENSE) y
[PROVENANCE.md](PROVENANCE.md).
