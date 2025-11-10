# Micro:bit Serial NeoPixel Meter (Variant)

Use this alternative script if you want a clean copy while retaining the original `microbib.md`.

```typescript
// Neopixel LED strip (24 LED, Pin P2)
let strip = neopixel.create(DigitalPin.P2, 24, NeoPixelMode.RGB)
let maxLevel = strip.length()
let tailBrightness = 0
let tailLeftIdx = Math.idiv(maxLevel - 1, 2)
let tailRightIdx = tailLeftIdx + (maxLevel % 2 == 0 ? 1 : 0)
const tailBaseBrightness = 255
const tailDecay = 15
const tailReturnSpeed = 1
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
    let level = Math.constrain(incomingHighLevel, 0, 255)
    let rows = Math.idiv(level, 51)
    let remainder = level % 51
    let column = 4
    for (let y = 0; y < 5; y++) {
        let brightness = 0
        if (y < rows) {
            brightness = 255
        } else if (y == rows) {
            brightness = Math.map(remainder, 0, 50, 0, 255)
        }
        let existing = led.pointBrightness(column, y)
        led.plotBrightness(column, y, Math.max(existing, brightness))
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
    let centerLeft = Math.idiv(maxLevel - 1, 2)
    let centerRight = centerLeft + (maxLevel % 2 == 0 ? 1 : 0)

    if (currentIndices.length === 0) {
        if (tailBrightness > 0) {
            tailBrightness = Math.max(0, tailBrightness - tailDecay)
        }
        if (tailLeftIdx < centerLeft) {
            tailLeftIdx = Math.min(centerLeft, tailLeftIdx + tailReturnSpeed)
        } else if (tailLeftIdx > centerLeft) {
            tailLeftIdx = Math.max(centerLeft, tailLeftIdx - tailReturnSpeed)
        }
        if (tailRightIdx > centerRight) {
            tailRightIdx = Math.max(centerRight, tailRightIdx - tailReturnSpeed)
        } else if (tailRightIdx < centerRight) {
            tailRightIdx = Math.min(centerRight, tailRightIdx + tailReturnSpeed)
        }
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

    if (minIdx < tailLeftIdx) {
        tailLeftIdx = minIdx
    } else {
        tailLeftIdx = Math.min(minIdx, tailLeftIdx + tailReturnSpeed)
    }
    if (maxIdx > tailRightIdx) {
        tailRightIdx = maxIdx
    } else {
        tailRightIdx = Math.max(maxIdx, tailRightIdx - tailReturnSpeed)
    }
    tailBrightness = tailBaseBrightness
}

function mapHighFromTail(level: number, total: number) {
    level = Math.constrain(level, 0, total)
    let indices: number[] = []
    let leftCursor = tailLeftIdx
    let rightCursor = tailRightIdx
    while (indices.length < level && (leftCursor >= 0 || rightCursor < total)) {
        if (leftCursor >= 0 && indices.length < level) {
            if (indices.indexOf(leftCursor) < 0) {
                indices.push(leftCursor)
            }
            leftCursor--
        }
        if (rightCursor < total && indices.length < level) {
            if (indices.indexOf(rightCursor) < 0) {
                indices.push(rightCursor)
            }
            rightCursor++
        }
        if (leftCursor < 0 && rightCursor >= total) {
            break
        }
    }
    return indices
}

function colorFromLevel(level: number, r: number, g: number, b: number) {
    level = Math.constrain(level, 0, 255)
    if (level <= 0) {
        return neopixel.rgb(0, 0, 0)
    }
    return neopixel.rgb(
        Math.idiv(r * level, 255),
        Math.idiv(g * level, 255),
        Math.idiv(b * level, 255)
    )
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
    if (lowIndices.length === 0 && lowCount === 0) {
        lowIndices = []
    }

    let lowColor = colorFromLevel(incomingLevel, 120, 0, 0)
    for (let idx of lowIndices) {
        strip.setPixelColor(idx, lowColor)
    }

    if (highBandAvailable) {
        let highCount = Math.map(incomingHighLevel, 0, 255, 0, maxLevel)
        highCount = Math.constrain(highCount, 0, maxLevel)
        let highIndices = mapHighFromTail(highCount, maxLevel)
        let highColor = colorFromLevel(incomingHighLevel, 0, 120, 255)
        for (let idx of highIndices) {
            strip.setPixelColor(idx, highColor)
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
```

