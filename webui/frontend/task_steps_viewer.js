/* Task steps viewer helpers for Task Viewer modal */
'use strict';

(function bootstrapTaskStepsViewer(global) {
  function createElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDateTime(value) {
    const date = parseDate(value);
    return date ? date.toLocaleString() : '—';
  }

  function formatInteger(value) {
    if (!Number.isFinite(value)) return '0';
    return Math.round(value).toLocaleString();
  }

  function formatDurationMs(durationMs) {
    if (!Number.isFinite(durationMs) || durationMs < 0) return '—';
    const totalSeconds = Math.floor(durationMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    }
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
  }

  function formatTaskDuration(startedAt, finishedAt) {
    const started = parseDate(startedAt);
    if (!started) return '—';
    const finished = parseDate(finishedAt);
    const endTime = finished ? finished.getTime() : Date.now();
    return formatDurationMs(endTime - started.getTime());
  }

  function formatNs(nsValue) {
    if (!Number.isFinite(nsValue) || nsValue < 0) return '—';
    return formatDurationMs(nsValue / 1000000);
  }

  function clipText(value, maxLength) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    if (!text) return '—';
    if (text.length <= maxLength) return text;
    return `${text.slice(0, Math.max(0, maxLength - 1))}…`;
  }

  function extractUsage(entry) {
    const response = entry && typeof entry.response === 'object' ? entry.response : {};
    const usage = response && typeof response.usage === 'object' ? response.usage : {};

    const promptTokens = Number(
      response.prompt_eval_count ?? response.prompt_tokens ?? usage.prompt_eval_count ?? usage.prompt_tokens ?? 0,
    ) || 0;
    const completionTokens = Number(
      response.eval_count ?? response.completion_tokens ?? usage.eval_count ?? usage.completion_tokens ?? 0,
    ) || 0;
    const totalTokens = Number(response.total_tokens ?? usage.total_tokens ?? (promptTokens + completionTokens)) || 0;

    return { promptTokens, completionTokens, totalTokens };
  }

  function extractToolInfo(entry) {
    const response = entry && typeof entry.response === 'object' ? entry.response : {};
    const message = response && typeof response.message === 'object' ? response.message : {};
    const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    const names = toolCalls
      .map(call => {
        const fn = call && typeof call.function === 'object' ? call.function : {};
        return fn.name || call.name || '';
      })
      .filter(Boolean);
    return {
      count: names.length,
      names,
    };
  }

  function extractPreview(entry, toolInfo) {
    const response = entry && typeof entry.response === 'object' ? entry.response : {};
    const message = response && typeof response.message === 'object' ? response.message : {};

    if (toolInfo.count > 0) {
      return `Tool calls: ${toolInfo.names.join(', ')}`;
    }

    if (typeof message.content === 'string' && message.content.trim()) {
      return clipText(message.content, 180);
    }

    if (typeof message.thinking === 'string' && message.thinking.trim()) {
      return clipText(message.thinking, 180);
    }

    if (response.done_reason) {
      return `done_reason=${response.done_reason}`;
    }

    return '—';
  }

  function parseStep(rawLine, index) {
    try {
      const entry = JSON.parse(rawLine);
      const usage = extractUsage(entry);
      const toolInfo = extractToolInfo(entry);
      return {
        index: index + 1,
        rawLine,
        entry,
        parseError: '',
        ts: parseDate(entry.ts),
        tsLabel: formatDateTime(entry.ts),
        modelDurationNs: Number(entry?.response?.total_duration) || 0,
        promptTokens: usage.promptTokens,
        completionTokens: usage.completionTokens,
        totalTokens: usage.totalTokens,
        toolCallCount: toolInfo.count,
        toolCallNames: toolInfo.names,
        preview: extractPreview(entry, toolInfo),
      };
    } catch (error) {
      return {
        index: index + 1,
        rawLine,
        entry: null,
        parseError: error && error.message ? error.message : 'Invalid JSON',
        ts: null,
        tsLabel: '—',
        modelDurationNs: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        toolCallCount: 0,
        toolCallNames: [],
        preview: 'Raw line fallback',
      };
    }
  }

  function deriveStepDurations(task, steps) {
    const taskStarted = parseDate(task && task.started_at);
    let prevTsMs = taskStarted ? taskStarted.getTime() : null;

    steps.forEach(step => {
      if (step.modelDurationNs > 0) {
        step.stepDurationMs = step.modelDurationNs / 1000000;
      } else if (step.ts && prevTsMs !== null) {
        step.stepDurationMs = Math.max(0, step.ts.getTime() - prevTsMs);
      } else {
        step.stepDurationMs = NaN;
      }

      if (step.ts) {
        prevTsMs = step.ts.getTime();
      }
    });

    return steps;
  }

  function renderSummaryCard(label, value, subvalue) {
    const card = createElement('div', 'task-steps-summary-card');
    card.appendChild(createElement('div', 'task-steps-summary-label', label));
    card.appendChild(createElement('div', 'task-steps-summary-value', value));
    if (subvalue) {
      card.appendChild(createElement('div', 'task-steps-note', subvalue));
    }
    return card;
  }

  function renderSummaryGrid(task, searchResult, steps) {
    const grid = createElement('div', 'task-steps-summary-grid');
    const totalPrompt = steps.reduce((sum, step) => sum + step.promptTokens, 0);
    const totalCompletion = steps.reduce((sum, step) => sum + step.completionTokens, 0);
    const totalTokens = steps.reduce((sum, step) => sum + step.totalTokens, 0);
    const totalToolCalls = steps.reduce((sum, step) => sum + step.toolCallCount, 0);

    grid.appendChild(renderSummaryCard('Task duration', formatTaskDuration(task?.started_at, task?.finished_at)));
    grid.appendChild(renderSummaryCard('Steps', formatInteger(steps.length), searchResult?.truncated ? `Showing first ${steps.length}` : 'All matched log lines'));
    grid.appendChild(renderSummaryCard('Total tokens', formatInteger(totalTokens), `Prompt ${formatInteger(totalPrompt)} · Completion ${formatInteger(totalCompletion)}`));
    grid.appendChild(renderSummaryCard('Tool calls', formatInteger(totalToolCalls), totalToolCalls > 0 ? 'Detected from response.message.tool_calls' : 'No tool calls in matched steps'));
    grid.appendChild(renderSummaryCard('Log file', searchResult?.file || '—', `${formatInteger(searchResult?.count || steps.length)} matched entr${(searchResult?.count || steps.length) === 1 ? 'y' : 'ies'}`));

    return grid;
  }

  function renderStepsTable(steps) {
    const wrap = createElement('div', 'task-steps-table-wrap');
    const table = createElement('table', 'task-steps-table');
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>#</th><th>Finished</th><th>Step time</th><th>Prompt</th><th>Completion</th><th>Total</th><th>Tools</th><th>Preview</th></tr>';
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    steps.forEach(step => {
      const tr = document.createElement('tr');
      [
        String(step.index),
        step.tsLabel,
        formatDurationMs(step.stepDurationMs),
        formatInteger(step.promptTokens),
        formatInteger(step.completionTokens),
        formatInteger(step.totalTokens),
        step.toolCallCount > 0 ? `${step.toolCallCount} (${clipText(step.toolCallNames.join(', '), 60)})` : '0',
      ].forEach(value => {
        tr.appendChild(createElement('td', '', value));
      });

      const previewCell = createElement('td', 'task-steps-preview', step.preview);
      tr.appendChild(previewCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function primitiveNode(key, className, valueText) {
    const line = createElement('div', 'json-tree-line');
    if (key !== null && key !== undefined) {
      const keySpan = createElement('span', 'json-key', `${key}: `);
      line.appendChild(keySpan);
    }
    line.appendChild(createElement('span', className, valueText));
    return line;
  }

  function jsonTreeNode(value, key, depth) {
    if (value === null) return primitiveNode(key, 'json-null', 'null');
    if (typeof value === 'string') return primitiveNode(key, 'json-string', JSON.stringify(value));
    if (typeof value === 'number') return primitiveNode(key, 'json-number', String(value));
    if (typeof value === 'boolean') return primitiveNode(key, 'json-boolean', String(value));

    if (Array.isArray(value)) {
      const details = document.createElement('details');
      if (depth <= 1) details.open = true;
      const summary = document.createElement('summary');
      const line = createElement('span', 'json-tree-summary-line');
      if (key !== null && key !== undefined) {
        line.appendChild(createElement('span', 'json-key', `${key}: `));
      }
      line.appendChild(createElement('span', 'json-brace', `[${value.length}]`));
      summary.appendChild(line);
      details.appendChild(summary);
      const children = createElement('div', 'json-tree-children');
      if (!value.length) {
        children.appendChild(createElement('div', 'json-tree-line', '[]'));
      } else {
        value.forEach((item, index) => children.appendChild(jsonTreeNode(item, index, depth + 1)));
      }
      details.appendChild(children);
      return details;
    }

    if (typeof value === 'object') {
      const entries = Object.entries(value);
      const details = document.createElement('details');
      if (depth <= 1) details.open = true;
      const summary = document.createElement('summary');
      const line = createElement('span', 'json-tree-summary-line');
      if (key !== null && key !== undefined) {
        line.appendChild(createElement('span', 'json-key', `${key}: `));
      }
      line.appendChild(createElement('span', 'json-brace', `{${entries.length}}`));
      summary.appendChild(line);
      details.appendChild(summary);
      const children = createElement('div', 'json-tree-children');
      if (!entries.length) {
        children.appendChild(createElement('div', 'json-tree-line', '{}'));
      } else {
        entries.forEach(([childKey, childValue]) => children.appendChild(jsonTreeNode(childValue, childKey, depth + 1)));
      }
      details.appendChild(children);
      return details;
    }

    return primitiveNode(key, 'json-string', JSON.stringify(String(value)));
  }

  function renderJsonTree(value) {
    const root = createElement('div', 'json-tree');
    root.appendChild(jsonTreeNode(value, null, 0));
    return root;
  }

  function renderStepEntries(steps) {
    const list = createElement('div', 'task-steps-records');

    steps.forEach(step => {
      const details = createElement('details', 'task-step-entry');
      if (step.index === 1) details.open = true;

      const summary = document.createElement('summary');
      const title = createElement('div', 'task-step-entry-title');
      title.appendChild(createElement('span', '', `Step ${step.index}`));
      title.appendChild(createElement('span', 'task-steps-note', formatDurationMs(step.stepDurationMs)));
      summary.appendChild(title);

      const meta = createElement('div', 'task-step-entry-meta');
      meta.appendChild(createElement('span', '', `Finished ${step.tsLabel}`));
      meta.appendChild(createElement('span', '', `Tokens ${formatInteger(step.totalTokens)}`));
      meta.appendChild(createElement('span', '', `Tools ${formatInteger(step.toolCallCount)}`));
      summary.appendChild(meta);
      summary.appendChild(createElement('div', 'task-step-entry-preview', step.preview));
      details.appendChild(summary);

      const body = createElement('div', 'task-step-entry-body');
      if (step.parseError) {
        body.appendChild(createElement('div', 'task-steps-note', `JSON parse error: ${step.parseError}`));
        body.appendChild(createElement('pre', 'json-raw-fallback', step.rawLine));
      } else {
        body.appendChild(renderJsonTree(step.entry));
      }
      details.appendChild(body);
      list.appendChild(details);
    });

    return list;
  }

  function renderEmptyState() {
    return createElement('div', 'task-steps-empty', 'No matching log entries found.');
  }

  function renderStepsView(task, searchResult) {
    const lines = Array.isArray(searchResult?.lines) ? searchResult.lines : [];
    const steps = deriveStepDurations(task || {}, lines.map(parseStep));

    const bodyNode = createElement('div', 'task-steps-view');
    bodyNode.appendChild(renderSummaryGrid(task || {}, searchResult || {}, steps));

    const tableTitle = createElement('div', 'task-steps-summary-label', 'Step summary');
    bodyNode.appendChild(tableTitle);
    if (steps.length) {
      bodyNode.appendChild(renderStepsTable(steps));
    } else {
      bodyNode.appendChild(renderEmptyState());
    }

    const entriesTitle = createElement('div', 'task-steps-summary-label', 'Step records');
    bodyNode.appendChild(entriesTitle);
    if (steps.length) {
      bodyNode.appendChild(renderStepEntries(steps));
    } else {
      bodyNode.appendChild(renderEmptyState());
    }

    const subtitleParts = [searchResult?.file || '—', `${searchResult?.count ?? steps.length} entr${(searchResult?.count ?? steps.length) === 1 ? 'y' : 'ies'}`];
    if (searchResult?.truncated) {
      subtitleParts.push(`showing first ${steps.length}`);
    }

    return {
      title: `Task ${task?.id || ''} steps`,
      subtitle: subtitleParts.join(' · '),
      bodyNode,
    };
  }

  global.TaskStepsViewer = {
    renderJsonTree,
    renderStepsView,
  };
})(window);
