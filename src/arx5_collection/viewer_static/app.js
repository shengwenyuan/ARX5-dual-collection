const app = document.querySelector("#app")
const form = document.querySelector("#dataset-form")
const pathInput = document.querySelector("#dataset-path")
const template = document.querySelector("#episode-template")
let datasetPath = sessionStorage.getItem("arx5-dataset-path") || "/mnt/cfs/data/swy/folding_the_cloth_20260821_20260822_v1"

const formatTime = seconds => {
  const value = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(Math.floor(value % 60)).padStart(2, "0")}`
}

const api = async url => {
  const response = await fetch(url)
  const body = await response.json()
  if (!response.ok) throw new Error(body.error || response.statusText)
  return body
}

const showError = error => {
  app.innerHTML = `<div class="error"><strong>Could not open dataset</strong><span>${error.message}</span></div>`
}

const renderDataset = async () => {
  app.innerHTML = '<div class="loading">Reading dataset index…</div>'
  try {
    const data = await api(`/api/dataset?path=${encodeURIComponent(datasetPath)}`)
    datasetPath = data.path
    sessionStorage.setItem("arx5-dataset-path", data.path)
    pathInput.value = data.path
    history.replaceState(null, "", "#/")
    app.innerHTML = `
      <p class="eyebrow">LeRobot v2.1 · ${data.fps} FPS</p>
      <h1>${data.name}</h1>
      <p class="subhead">Browse retained training episodes and inspect the exact OpenPI action target at every anchor.</p>
      <section class="stats">
        <div class="stat"><strong>${data.total_episodes.toLocaleString()}</strong><span>Episodes</span></div>
        <div class="stat"><strong>${data.total_frames.toLocaleString()}</strong><span>Frames</span></div>
        <div class="stat"><strong>${data.total_videos.toLocaleString()}</strong><span>Videos</span></div>
        <div class="stat"><strong>${(data.total_frames / data.fps / 60).toFixed(1)}</strong><span>Minutes</span></div>
      </section>
      <section class="grid" id="episode-grid"></section>`
    const grid = document.querySelector("#episode-grid")
    for (const episode of data.episodes) {
      const node = template.content.cloneNode(true)
      const card = node.querySelector("button")
      card.querySelector("img").src = episode.thumbnail_url
      card.querySelector("img").alt = `Episode ${episode.episode_index} thumbnail`
      card.querySelector(".duration").textContent = formatTime(episode.duration_s)
      card.querySelector("strong").textContent = `Episode ${String(episode.episode_index).padStart(3, "0")}`
      card.querySelector(".task").textContent = episode.tasks.join(", ")
      card.querySelector(".meta").textContent = `${episode.length.toLocaleString()} training samples`
      card.addEventListener("click", () => renderEpisode(episode.episode_index))
      grid.append(node)
    }
  } catch (error) {
    showError(error)
  }
}

const colors = ["#4df0a5", "#71a7ff", "#ffca65", "#fb7b8e", "#b795ff", "#53d8dc"]

const actionChart = (series, labels, title) => {
  const width = 760
  const height = 210
  const pad = 28
  const values = series.flat()
  const low = Math.min(...values)
  const high = Math.max(...values)
  const spread = high - low || 1
  const point = (value, index, count) => {
    const x = pad + index * (width - pad * 2) / Math.max(1, count - 1)
    const y = height - pad - (value - low) * (height - pad * 2) / spread
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }
  const lines = series.map((valuesForMotor, index) => `<polyline points="${valuesForMotor.map((value, step) => point(value, step, valuesForMotor.length)).join(" ")}" fill="none" stroke="${colors[index % colors.length]}" stroke-width="2" vector-effect="non-scaling-stroke"/>`).join("")
  const legend = labels.map((label, index) => `<span style="--series:${colors[index % colors.length]}">${label}</span>`).join("")
  return `<article class="chart"><div class="chart-title"><strong>${title}</strong><small>${low.toFixed(4)} → ${high.toFixed(4)}</small></div><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><line x1="${pad}" y1="${height / 2}" x2="${width - pad}" y2="${height / 2}"/><line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}"/>${lines}</svg><div class="legend">${legend}</div></article>`
}

const nearestPoint = (points, timestamp) => {
  let low = 0
  let high = points.length - 1
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if (points[middle][2] < timestamp) low = middle + 1
    else high = middle
  }
  if (low > 0 && timestamp - points[low - 1][2] < points[low][2] - timestamp) return low - 1
  return low
}

const createSamplePlayer = (episode, timeline, loadAnchor) => {
  const video = document.querySelector("#sample-video")
  const canvas = document.querySelector("#sample-timeline")
  const pointLabel = document.querySelector("#video-point-label")
  const windowLabel = document.querySelector("#sample-window-label")
  const localSamples = document.querySelector("#local-samples")
  const playButton = document.querySelector("#play-sample-clip")
  const progress = document.querySelector("#clip-progress")
  const clipTime = document.querySelector("#clip-time")
  const clipWindowLabel = document.querySelector("#clip-window-label")
  const points = timeline.points
  const duration = episode.duration_s
  let active = 0
  let clipStart = 0
  let clipEnd = episode.action_horizon / episode.fps
  let availableSteps = episode.action_horizon

  const playbackOffset = () => Math.min(
    clipEnd - clipStart,
    Math.max(0, video.currentTime - clipStart),
  )

  const updatePlayback = () => {
    const offset = playbackOffset()
    progress.value = offset
    clipTime.textContent = `${offset.toFixed(2)} / ${(clipEnd - clipStart).toFixed(2)} s`
    if (!video.paused) playButton.textContent = "Pause action clip"
    else if (video.currentTime >= clipEnd - 0.01) playButton.textContent = "Replay action clip"
    else playButton.textContent = "Play action clip"
  }

  const updatePointLabel = () => {
    const point = points[active]
    pointLabel.innerHTML = `<strong>ACTION CLIP · ANCHOR ${point[1].toLocaleString()}</strong><span>${clipStart.toFixed(2)}–${clipEnd.toFixed(2)} s · cursor ${video.currentTime.toFixed(2)} s</span>`
  }

  const draw = () => {
    const ratio = window.devicePixelRatio || 1
    const width = canvas.clientWidth
    const height = canvas.clientHeight
    canvas.width = Math.round(width * ratio)
    canvas.height = Math.round(height * ratio)
    const context = canvas.getContext("2d")
    context.scale(ratio, ratio)
    context.clearRect(0, 0, width, height)
    context.fillStyle = "#111715"
    context.fillRect(0, 0, width, height)
    const xAt = timestamp => Math.min(width, Math.max(0, timestamp / duration * width))
    context.fillStyle = "rgba(113, 167, 255, .22)"
    context.fillRect(xAt(clipStart), 0, Math.max(2, xAt(clipEnd) - xAt(clipStart)), height)
    context.fillStyle = "rgba(77, 240, 165, .78)"
    points.forEach((point, index) => {
      context.fillRect(xAt(point[2]), 14 + index % 5 * 6, 1.25, 2)
    })
    context.fillStyle = "#ffca65"
    context.fillRect(xAt(clipStart) - 1, 0, 3, height)
    context.fillStyle = "#eef2ef"
    context.fillRect(xAt(Math.min(clipEnd, Math.max(clipStart, video.currentTime))) - 0.5, 0, 1, height)
    context.fillStyle = "#89948f"
    context.font = "11px Inter, system-ui, sans-serif"
    context.fillText("0:00", 8, height - 8)
    const endLabel = formatTime(duration)
    context.fillText(endLabel, width - context.measureText(endLabel).width - 8, height - 8)
  }

  const renderLocalSamples = () => {
    const width = 41
    const start = Math.max(0, Math.min(points.length - width, active - Math.floor(width / 2)))
    const end = Math.min(points.length, start + width)
    localSamples.innerHTML = points.slice(start, end).map((point, offset) => {
      const frame = start + offset
      const current = frame === active ? " current" : ""
      const inChunk = frame >= active && frame < active + episode.action_horizon ? " in-chunk" : ""
      return `<button class="sample-point${current}${inChunk}" data-frame="${frame}" title="dataset index ${point[0]} · frame ${point[1]} · ${point[2].toFixed(3)} s"><i></i><span>${frame}</span></button>`
    }).join("")
    localSamples.querySelectorAll("button").forEach(button => {
      button.addEventListener("click", () => select(Number(button.dataset.frame)))
    })
    windowLabel.textContent = `Frames ${start}–${end - 1}`
  }

  const show = frame => {
    active = Math.max(0, Math.min(points.length - 1, frame))
    const frameIndex = points[active][1]
    availableSteps = Math.min(episode.action_horizon, points.length - active)
    clipStart = frameIndex / episode.fps
    clipEnd = Math.min(duration, clipStart + availableSteps / episode.fps)
    progress.max = clipEnd - clipStart
    progress.value = 0
    clipWindowLabel.textContent = `Frames ${frameIndex}–${frameIndex + availableSteps - 1} · ${availableSteps}/${episode.action_horizon} recorded steps`
    document.querySelector("#anchor-range").value = active
    document.querySelector("#anchor-number").value = active
    updatePointLabel()
    updatePlayback()
    draw()
    renderLocalSamples()
  }

  const select = (frame, shouldLoad = true) => {
    video.pause()
    show(frame)
    video.currentTime = clipStart
    updatePointLabel()
    updatePlayback()
    draw()
    if (shouldLoad) loadAnchor(active)
  }

  const toggleClip = () => {
    if (!video.paused) {
      video.pause()
      return
    }
    if (video.currentTime < clipStart || video.currentTime >= clipEnd - 0.01) video.currentTime = clipStart
    video.play()
  }

  video.addEventListener("timeupdate", () => {
    if (!video.paused && video.currentTime >= clipEnd - 0.002) {
      video.pause()
      video.currentTime = clipEnd
    }
    updatePointLabel()
    updatePlayback()
    draw()
  })
  video.addEventListener("play", updatePlayback)
  video.addEventListener("pause", updatePlayback)
  video.addEventListener("click", toggleClip)
  playButton.addEventListener("click", toggleClip)
  progress.addEventListener("input", () => {
    video.pause()
    video.currentTime = clipStart + Number(progress.value)
    updatePointLabel()
    updatePlayback()
    draw()
  })
  canvas.addEventListener("click", event => {
    const bounds = canvas.getBoundingClientRect()
    const timestamp = (event.clientX - bounds.left) / bounds.width * duration
    select(nearestPoint(points, timestamp))
  })
  new ResizeObserver(draw).observe(canvas)
  show(0)
  return { select }
}

const renderChunk = chunk => {
  const leftJointDelta = Array.from({ length: 6 }, (_, joint) => chunk.actions.map(action => action[joint] - chunk.state[joint]))
  const rightJointDelta = Array.from({ length: 6 }, (_, joint) => chunk.actions.map(action => action[joint + 7] - chunk.state[joint + 7]))
  const grippers = [chunk.actions.map(action => action[6]), chunk.actions.map(action => action[13])]
  const padding = chunk.padding_steps ? `<span class="warning">${chunk.padding_steps} padded tail steps</span>` : '<span class="ready">Full 50-step target</span>'
  document.querySelector("#sample-content").innerHTML = `
    <div class="sample-heading"><div><p class="eyebrow">Training sample</p><h2>Anchor ${chunk.frame_index.toLocaleString()}</h2></div><div class="sample-facts"><span>${chunk.timestamp.toFixed(2)} s</span><span>${chunk.action_horizon} × 14 actions</span>${padding}</div></div>
    <section class="observation-grid">${chunk.images.map(image => `<figure><img src="${image.url}" alt="${image.label} at frame ${chunk.frame_index}"><figcaption>${image.label}</figcaption></figure>`).join("")}</section>
    <section class="state"><h3>Current state · 14D</h3><div>${chunk.state.map((value, index) => `<span><small>${chunk.motor_names[index]}</small>${value.toFixed(4)}</span>`).join("")}</div></section>
    <section class="charts">
      ${actionChart(leftJointDelta, chunk.motor_names.slice(0, 6), "Left joint delta · 50 steps")}
      ${actionChart(rightJointDelta, chunk.motor_names.slice(7, 13), "Right joint delta · 50 steps")}
      ${actionChart(grippers, [chunk.motor_names[6], chunk.motor_names[13]], "Absolute gripper · 50 steps")}
    </section>`
}

const renderEpisode = async index => {
  app.innerHTML = '<div class="loading">Reading episode…</div>'
  try {
    const [episode, timeline] = await Promise.all([
      api(`/api/episode?path=${encodeURIComponent(datasetPath)}&episode=${index}`),
      api(`/api/sample-timeline?path=${encodeURIComponent(datasetPath)}&episode=${index}`),
    ])
    history.replaceState(null, "", `#/episode/${index}`)
    app.innerHTML = `
      <a class="back" href="#/">← All episodes</a>
      <p class="eyebrow">${episode.tasks.join(" · ")}</p>
      <h1>Episode ${String(episode.episode_index).padStart(3, "0")}</h1>
      <p class="subhead">${episode.training_samples.toLocaleString()} training anchors · ${formatTime(episode.duration_s)} · ${episode.action_horizon}-step horizon</p>
      <section class="provenance"><strong>Training snapshot</strong><span>${episode.provenance.detail}</span><div><i></i></div></section>
      <section class="sample-player">
        <div class="video-stage">
          <video id="sample-video" src="${episode.video.url}" preload="metadata" playsinline></video>
          <div id="video-point-label" class="video-point-label"></div>
          <span class="camera-label">${episode.video.label}</span>
        </div>
        <div class="clip-controls">
          <button id="play-sample-clip" type="button">Play action clip</button>
          <input id="clip-progress" type="range" min="0" max="1" value="0" step="0.001">
          <span id="clip-time">0.00 / 1.00 s</span>
          <span id="clip-window-label"></span>
        </div>
        <div class="timeline-heading"><div><strong>Actual training anchors</strong><span>${timeline.points.length.toLocaleString()} Parquet rows mapped to training-video timestamps</span></div><span id="sample-window-label"></span></div>
        <canvas id="sample-timeline" height="74"></canvas>
        <div class="sampling-legend"><span class="retained-key">Training anchor</span><span class="current-key">Selected anchor</span><span class="playback-key">Clip playback</span><span class="chunk-key">50-step action target</span></div>
        <div id="local-samples" class="local-samples"></div>
      </section>
      <section class="anchor-nav">
        <button id="previous-anchor">−1</button>
        <input id="anchor-range" type="range" min="0" max="${episode.training_samples - 1}" value="0" step="1">
        <button id="next-anchor">+1</button>
        <label>Frame <input id="anchor-number" type="number" min="0" max="${episode.training_samples - 1}" value="0"></label>
      </section>
      <section id="sample-content"><div class="loading">Building action chunk…</div></section>`
    const range = document.querySelector("#anchor-range")
    const number = document.querySelector("#anchor-number")
    let request = 0
    let timer
    const loadAnchor = async frame => {
      const bounded = Math.max(0, Math.min(episode.training_samples - 1, Number(frame)))
      range.value = bounded
      number.value = bounded
      const current = ++request
      document.querySelector("#sample-content").classList.add("updating")
      try {
        const chunk = await api(`/api/action-chunk?path=${encodeURIComponent(datasetPath)}&episode=${index}&frame=${bounded}`)
        if (current === request) renderChunk(chunk)
      } catch (error) {
        if (current === request) showError(error)
      } finally {
        document.querySelector("#sample-content")?.classList.remove("updating")
      }
    }
    const samplePlayer = createSamplePlayer(episode, timeline, loadAnchor)
    const schedule = frame => {
      clearTimeout(timer)
      timer = setTimeout(() => samplePlayer.select(Number(frame)), 100)
    }
    range.addEventListener("input", () => {
      number.value = range.value
      schedule(range.value)
    })
    number.addEventListener("change", () => samplePlayer.select(Number(number.value)))
    document.querySelector("#previous-anchor").addEventListener("click", () => samplePlayer.select(Number(range.value) - 1))
    document.querySelector("#next-anchor").addEventListener("click", () => samplePlayer.select(Number(range.value) + 1))
    samplePlayer.select(0)
  } catch (error) {
    showError(error)
  }
}

form.addEventListener("submit", event => {
  event.preventDefault()
  datasetPath = pathInput.value.trim()
  renderDataset()
})

window.addEventListener("hashchange", () => {
  const match = location.hash.match(/^#\/episode\/(\d+)$/)
  if (match) renderEpisode(Number(match[1]))
  else renderDataset()
})

const initialEpisode = location.hash.match(/^#\/episode\/(\d+)$/)
pathInput.value = datasetPath
if (initialEpisode) renderEpisode(Number(initialEpisode[1]))
else renderDataset()
