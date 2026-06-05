# App-Icon-Quellen (Android)

Aus diesen Dateien generiert `@capacitor/assets` die Launcher-Icons. Der Aufruf
passiert automatisch in `deploy/build-android.sh` (`npx @capacitor/assets generate
--android`), weil `frontend/android/` nicht eingecheckt ist und bei jedem Build
neu erzeugt wird. **Diese Dateien hier sind die dauerhafte Quelle.**

| Datei                | Verwendung                                                        |
| -------------------- | ---------------------------------------------------------------- |
| `icon-only.png`      | Legacy-Launcher-Icon (Android < 8, vollflächig)                  |
| `icon-foreground.png`| Vordergrund des Adaptive Icons (Android 8+); Tool fügt 16,7 % Safe-Zone-Inset hinzu, daher randvoll |
| `icon-background.png`| Hintergrund des Adaptive Icons (flaches Navy `#08142B`)          |

## Herkunft / Neu erzeugen

Master ist `../../logo_android.png` (1254×1254). Das Original hat **weiße Ecken**
(abgerundetes Tile auf Weiß, kein Alpha). Für ein sauberes Icon wird der weiße
Rand per Flood-Fill durch Navy ersetzt; das Ergebnis dient als Legacy-Icon und als
randvolles Adaptive-Foreground. Reproduzierbar mit ImageMagick:

```bash
cd frontend
NAVY="#08142B"; SRC=../logo_android.png
# Weißen Eckrand durch Navy ersetzen (zusammenhängender Flood-Fill aus allen 4 Ecken):
magick "$SRC" -alpha off -fill "$NAVY" -fuzz 14% \
  -draw "color 0,0 floodfill"       -draw "color 1253,0 floodfill" \
  -draw "color 0,1253 floodfill"    -draw "color 1253,1253 floodfill" \
  assets/icon-only.png
cp assets/icon-only.png assets/icon-foreground.png          # randvoll; Inset macht das Tool
magick -size 1254x1254 xc:"$NAVY" assets/icon-background.png
npx @capacitor/assets generate --android                    # schreibt nach android/.../res/mipmap-*
```

Soll das Logo geändert werden: `logo_android.png` ersetzen, obige Schritte erneut
ausführen, neu bauen.
