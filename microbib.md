# Micro:bit Serial NeoPixel Meter

1. Open Microsoft MakeCode, create a new project, and paste the script below.
2. Flash the project to your micro:bit and keep it connected to your PC over USB.
3. In the loopback monitor app, set the micro:bit COM port and `115200` baud, then click **Connect** to stream the low-band (and optionally high-band) values.

```typescript
// Neopixel LED strip (24 LED, Pin P2)
let strip = neopixel.create(DigitalPin.P2, 24, NeoPixelMode.RGB)
let maxLevel = strip.length()
let tailBrightness = 0
let tailLeftIdx = Math.idiv(maxLevel - 1, 2)
let tailRightIdx = tailLeftIdx + (maxLevel % 2 == 0 ? 1 : 0)
const tailBaseBrightness = 255
let incomingLevel = 0
let incomingHighLevel = 0
let highBandAvailable = false
let lastLevel = -1
let lastLevelChangeMs = 0
let strobeActive = false
let strobeEndMs = 0
const levelTolerance = 8

serial.redirectToUSB()
serial.setBaudRate(BaudRate.BaudRate115200)

serial.onDataReceived("\n", function () {
    let text = serial.readUntil(serial.delimiters(Delimiters.NewLine))
    if (text.length <= 0) {
        return
    }
    let parts = text.split(",")
    let value = parseInt(parts[0].trim())
    if (!(value >= 0)) {
        return
    }
    value = Math.constrain(value, 0, 255)
    incomingLevel = value

    if (parts.length > 1) {
        let highText = parts[1].trim()
        let parsedHigh = parseInt(highText)
        if (parsedHigh >= 0) {
            incomingHighLevel = Math.constrain(parsedHigh, 0, 255)
            highBandAvailable = true
        } else {
            incomingHighLevel = 0
            highBandAvailable = false
        }
    } else {
        incomingHighLevel = 0
        highBandAvailable = false
    }
})

function updateMatrixFromTail() {
    let level = incomingLevel
    let rows = Math.idiv(level, 51)
    let remainder = level % 51
    for (let y = 0; y < 5; y++) {
        let brightness = 0
        if (4 - y < rows) {
            brightness = 255
        } else if (4 - y == rows) {
            brightness = Math.map(remainder, 0, 50, 0, 255)
        }
        for (let x = 0; x < 5; x++) {
            led.plotBrightness(x, y, brightness)
        }
    }
}

function applyHighBandOverlay() {
    if (!highBandAvailable) {
        return
    }
    let overlay = Math.constrain(incomingHighLevel, 0, 255)
    for (let x = 0; x < 5; x++) {
        let existing = led.pointBrightness(x, 0)
        led.plotBrightness(x, 0, Math.max(existing, overlay))
    }
}

function strobeMatrix(now: number) {
    let onState = Math.idiv(now, 100) % 2 == 0
    let brightness = onState ? 255 : 0
    for (let y = 0; y < 5; y++) {
        for (let x = 0; x < 5; x++) {
            led.plotBrightness(x, y, brightness)
        }
    }
}

function mapSymmetric(level: number, total: number) {
    level = Math.constrain(level, 0, total)
    let centerLeft = Math.idiv(total - 1, 2)
    let centerRight = centerLeft + (total % 2 == 0 ? 1 : 0)
    let countPerSide = Math.idiv(level, 2)
    let remainder = level % 2
    let indices: number[] = []
    for (let i = 0; i < countPerSide; i++) {
        let leftIdx = centerLeft - i
        let rightIdx = centerRight + i
        if (leftIdx >= 0) {
            indices.push(leftIdx)
        }
        if (rightIdx < total) {
            indices.push(rightIdx)
        }
    }
    if (remainder > 0) {
        if (total % 2 == 1) {
            indices.push(centerLeft)
        } else {
            let nextRight = centerRight + countPerSide
            if (nextRight < total) {
                indices.push(nextRight)
            }
        }
    }
    return indices
}

function updateTailFromIndices(currentIndices: number[]) {
    if (currentIndices.length === 0) {
        tailBrightness = 0
        return
    }
    let minIdx = currentIndices[0]
    let maxIdx = currentIndices[0]
    for (let idx of currentIndices) {
        if (idx < minIdx) {
            minIdx = idx
        }
        if (idx > maxIdx) {
            maxIdx = idx
        }
    }
    tailLeftIdx = minIdx
    tailRightIdx = maxIdx
    tailBrightness = tailBaseBrightness
}

basic.forever(function () {
    let now = control.millis()
    if (lastLevel < 0) {
        lastLevel = incomingLevel
        lastLevelChangeMs = now
    }

    if (Math.abs(incomingLevel - lastLevel) > levelTolerance) {
        lastLevel = incomingLevel
        lastLevelChangeMs = now
        strobeActive = false
    } else if (!strobeActive && now - lastLevelChangeMs >= 1000) {
        strobeActive = true
        strobeEndMs = now + 250
    }

    if (strobeActive) {
        strobeMatrix(now)
        if (now >= strobeEndMs) {
            strobeActive = false
            lastLevelChangeMs = now
            lastLevel = incomingLevel
        }
    } else {
        updateMatrixFromTail()
        applyHighBandOverlay()
    }

    let lowCount = Math.map(incomingLevel, 0, 255, 0, maxLevel)
    lowCount = Math.constrain(lowCount, 0, maxLevel)
    strip.clear()

    let lowIndices = mapSymmetric(lowCount, maxLevel)
    for (let idx of lowIndices) {
        strip.setPixelColor(idx, neopixel.rgb(120, 0, 0))
    }

    if (highBandAvailable) {
        let highCount = Math.map(incomingHighLevel, 0, 255, 0, maxLevel)
        highCount = Math.constrain(highCount, 0, maxLevel)
        let highIndices = mapSymmetric(highCount, maxLevel)
        for (let idx of highIndices) {
            strip.setPixelColor(idx, neopixel.rgb(0, 120, 255))
        }
    }

    if (tailBrightness > 0) {
        strip.setPixelColor(tailLeftIdx, neopixel.rgb(tailBrightness, tailBrightness, 0))
        strip.setPixelColor(tailRightIdx, neopixel.rgb(tailBrightness, tailBrightness, 0))
    }

    strip.show()
    basic.pause(1)

    updateTailFromIndices(lowIndices)
})