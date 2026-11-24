const tickSteps = [1 / 60, 2 / 60, 5 / 60, 10 / 60, 15 / 60, 30 / 60, 1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720];
const shanghaiOffset = 8 * 60;
const tickSpacing = 150;

export function timelineTicks(start, end, plotWidth, visibleLeft, visibleWidth) {
  const pixelsPerMinute = plotWidth / (end - start);
  const step = tickSteps.find((minutes) => minutes * pixelsPerMinute >= tickSpacing) ?? tickSteps.at(-1);
  // Include one tick past each viewport edge so labels can scroll in without gaps.
  const first = Math.max(start, start + (visibleLeft - tickSpacing) / pixelsPerMinute);
  const last = Math.min(end, start + (visibleLeft + visibleWidth + tickSpacing) / pixelsPerMinute);
  const firstIndex = Math.ceil((first + shanghaiOffset) / step);
  const lastIndex = Math.floor((last + shanghaiOffset) / step);
  const ticks = [];
  for (let index = firstIndex; index <= lastIndex; index++) {
    ticks.push(index * step - shanghaiOffset);
  }
  return {ticks, withSeconds: step < 1};
}

export function timelineSecondAt(offset, plotWidth, start, end) {
  const fraction = Math.max(0, Math.min(1, offset / plotWidth));
  return Math.round((start + fraction * (end - start)) * 60);
}

export function measuredRange(anchor, cursor) {
  return {start: Math.min(anchor, cursor), end: Math.max(anchor, cursor)};
}

export function rangeDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return [hours && `${hours} 小时`, minutes && `${minutes} 分钟`, `${seconds % 60} 秒`].filter(Boolean).join(" ");
}
