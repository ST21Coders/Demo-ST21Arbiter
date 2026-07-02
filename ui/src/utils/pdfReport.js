const MARGIN = 48
const BOTTOM = MARGIN
const PAGE_SIZES = {
  portrait: { width: 612, height: 792 },
  landscape: { width: 792, height: 612 },
}

function safeText(value) {
  return String(value ?? '')
    .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function pdfEscape(value) {
  return safeText(value)
    .replace(/\\/g, '\\\\')
    .replace(/\(/g, '\\(')
    .replace(/\)/g, '\\)')
}

function textWidth(text, size) {
  return safeText(text).length * size * 0.52
}

function wrapText(text, size, maxWidth) {
  const words = safeText(text).split(/\s+/).filter(Boolean)
  if (!words.length) return ['']
  const lines = []
  let line = ''
  for (const word of words) {
    const next = line ? `${line} ${word}` : word
    if (textWidth(next, size) <= maxWidth || !line) {
      line = next
    } else {
      lines.push(line)
      line = word
    }
  }
  if (line) lines.push(line)
  return lines
}

function parseMarkdownTable(lines, startIndex) {
  const tableLines = []
  let index = startIndex
  while (index < lines.length) {
    const line = lines[index].trim()
    if (!line.startsWith('|') || !line.endsWith('|')) break
    tableLines.push(line)
    index += 1
  }
  const rows = tableLines
    .map(line => line.slice(1, -1).split('|').map(cell => cell.trim()))
    .filter(cells => !cells.every(cell => /^:?-{3,}:?$/.test(cell)))
  return { rows, nextIndex: index }
}

function parseBlocks(markdown) {
  const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const raw = lines[i]
    const line = raw.trim()
    if (!line) {
      i += 1
      continue
    }
    if (line.startsWith('|') && line.endsWith('|')) {
      const table = parseMarkdownTable(lines, i)
      if (table.rows.length) blocks.push({ type: 'table', rows: table.rows })
      i = table.nextIndex
      continue
    }
    if (line.startsWith('#')) {
      blocks.push({ type: 'heading', text: line.replace(/^#+\s*/, '') })
    } else if (/^\*\*[^*]+\*\*$/.test(line)) {
      blocks.push({ type: 'heading', text: line.replace(/\*\*/g, '') })
    } else if (/^[-*]\s+/.test(line) || /^\d+\.\s+/.test(line)) {
      blocks.push({ type: 'bullet', text: line.replace(/^[-*]\s+/, '').replace(/^\d+\.\s+/, '') })
    } else {
      blocks.push({ type: 'paragraph', text: line })
    }
    i += 1
  }
  return blocks
}

function shouldUseLandscape(blocks) {
  return blocks.some(block => {
    if (block.type !== 'table') return false
    const columnCount = Math.max(...block.rows.map(row => row.length))
    const longestRow = Math.max(...block.rows.map(row => row.join(' ').length))
    return columnCount >= 6 || longestRow > 180
  })
}

function makePdfBuilder(title, layout = 'portrait') {
  const pageSize = PAGE_SIZES[layout] || PAGE_SIZES.portrait
  const contentWidth = pageSize.width - MARGIN * 2
  const top = pageSize.height - MARGIN
  const pages = []
  let ops = []
  let y = top

  function addPage() {
    if (ops.length) pages.push(ops)
    ops = []
    y = top
    drawText(title, MARGIN, y, 9, { color: '475569' })
    y -= 18
    drawLine(MARGIN, y, pageSize.width - MARGIN, y, 'CBD5E1')
    y -= 18
  }

  function ensureSpace(height) {
    if (y - height < BOTTOM) addPage()
  }

  function drawText(text, x, textY, size = 10, options = {}) {
    const color = options.color || '0F172A'
    const [r, g, b] = color.match(/.{2}/g).map(hex => parseInt(hex, 16) / 255)
    ops.push(`${r.toFixed(3)} ${g.toFixed(3)} ${b.toFixed(3)} rg`)
    ops.push(`BT /F1 ${size} Tf ${x.toFixed(2)} ${textY.toFixed(2)} Td (${pdfEscape(text)}) Tj ET`)
  }

  function drawLine(x1, y1, x2, y2, color = 'E2E8F0') {
    const [r, g, b] = color.match(/.{2}/g).map(hex => parseInt(hex, 16) / 255)
    ops.push(`${r.toFixed(3)} ${g.toFixed(3)} ${b.toFixed(3)} RG`)
    ops.push(`0.5 w ${x1.toFixed(2)} ${y1.toFixed(2)} m ${x2.toFixed(2)} ${y2.toFixed(2)} l S`)
  }

  function drawRect(x, rectY, width, height, fill, stroke = 'CBD5E1') {
    const [fr, fg, fb] = fill.match(/.{2}/g).map(hex => parseInt(hex, 16) / 255)
    const [sr, sg, sb] = stroke.match(/.{2}/g).map(hex => parseInt(hex, 16) / 255)
    ops.push(`${fr.toFixed(3)} ${fg.toFixed(3)} ${fb.toFixed(3)} rg`)
    ops.push(`${sr.toFixed(3)} ${sg.toFixed(3)} ${sb.toFixed(3)} RG`)
    ops.push(`0.5 w ${x.toFixed(2)} ${rectY.toFixed(2)} ${width.toFixed(2)} ${height.toFixed(2)} re B`)
  }

  function addWrappedText(text, size, color, indent = 0, prefix = '') {
    const x = MARGIN + indent
    const width = contentWidth - indent
    const lines = wrapText(`${prefix}${text}`, size, width)
    ensureSpace(lines.length * (size + 4) + 8)
    for (const line of lines) {
      drawText(line, x, y, size, { color })
      y -= size + 4
    }
    y -= 3
  }

  function addTable(rows) {
    if (!rows.length) return
    const columnCount = Math.max(...rows.map(row => row.length))
    const columnWidth = contentWidth / columnCount
    const fontSize = columnCount >= 10 ? 5 : columnCount >= 8 ? 5.5 : columnCount >= 6 ? 6.2 : 7.5
    const cellPadding = columnCount >= 8 ? 3 : 4
    const maxLinesPerCell = columnCount >= 8 ? 5 : 6
    const normalizedRows = rows.map(row => Array.from({ length: columnCount }, (_, index) => row[index] || ''))

    y -= 2
    normalizedRows.forEach((row, rowIndex) => {
      const wrapped = row.map(cell => wrapText(cell, fontSize, columnWidth - cellPadding * 2).slice(0, maxLinesPerCell))
      const rowHeight = Math.max(18, Math.max(...wrapped.map(lines => lines.length)) * (fontSize + 2) + cellPadding * 2)
      ensureSpace(rowHeight + 8)
      const topY = y
      const rectY = topY - rowHeight
      row.forEach((_, colIndex) => {
        drawRect(
          MARGIN + colIndex * columnWidth,
          rectY,
          columnWidth,
          rowHeight,
          rowIndex === 0 ? 'F1F5F9' : 'FFFFFF',
          'CBD5E1',
        )
      })
      wrapped.forEach((cellLines, colIndex) => {
        let cellY = topY - cellPadding - fontSize
        cellLines.forEach(line => {
          drawText(line, MARGIN + colIndex * columnWidth + cellPadding, cellY, fontSize, {
            color: rowIndex === 0 ? '0F172A' : '334155',
          })
          cellY -= fontSize + 2
        })
      })
      y -= rowHeight
    })
    y -= 12
  }

  addPage()

  return {
    addBlock(block) {
      if (block.type === 'heading') {
        y -= 2
        addWrappedText(block.text, 13, '0F172A')
      } else if (block.type === 'bullet') {
        addWrappedText(block.text, 9.5, '334155', 12, '- ')
      } else if (block.type === 'table') {
        addTable(block.rows)
      } else {
        addWrappedText(block.text, 10, '334155')
      }
    },
    finish() {
      if (ops.length) pages.push(ops)
      return { pageOps: pages, pageSize }
    },
  }
}

function buildPdf(title, markdown) {
  const blocks = parseBlocks(markdown)
  const builder = makePdfBuilder(title, shouldUseLandscape(blocks) ? 'landscape' : 'portrait')
  blocks.forEach(block => builder.addBlock(block))
  const { pageOps, pageSize } = builder.finish()
  const objects = []
  const pageRefs = []

  function addObject(body) {
    objects.push(body)
    return objects.length
  }

  const catalogId = addObject('<< /Type /Catalog /Pages 2 0 R >>')
  const pagesId = addObject('')
  const fontId = addObject('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')

  pageOps.forEach(ops => {
    const stream = ops.join('\n')
    const contentId = addObject(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`)
    const pageId = addObject(`<< /Type /Page /Parent ${pagesId} 0 R /MediaBox [0 0 ${pageSize.width} ${pageSize.height}] /Resources << /Font << /F1 ${fontId} 0 R >> >> /Contents ${contentId} 0 R >>`)
    pageRefs.push(`${pageId} 0 R`)
  })
  objects[pagesId - 1] = `<< /Type /Pages /Kids [${pageRefs.join(' ')}] /Count ${pageRefs.length} >>`
  objects[catalogId - 1] = '<< /Type /Catalog /Pages 2 0 R >>'

  let pdf = '%PDF-1.4\n'
  const offsets = [0]
  objects.forEach((body, index) => {
    offsets.push(pdf.length)
    pdf += `${index + 1} 0 obj\n${body}\nendobj\n`
  })
  const xrefOffset = pdf.length
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  offsets.slice(1).forEach(offset => {
    pdf += `${String(offset).padStart(10, '0')} 00000 n \n`
  })
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF`
  return new Blob([pdf], { type: 'application/pdf' })
}

function slug(value) {
  return safeText(value).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 80) || 'arbiter-report'
}

export function downloadChatPdf({ title = 'ARBITER Report', content = '', filename } = {}) {
  const blob = buildPdf(title, content)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || `${slug(title)}.pdf`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
