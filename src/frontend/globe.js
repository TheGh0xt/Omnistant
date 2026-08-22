/* Memory globe.
 *
 * Every observation the agent holds becomes a point on a sphere you can turn
 * with a finger. It replaces a scrolling log on purpose: a dense list of
 * timestamps is exactly the kind of surface the target user bounces off.
 *
 * Positions are presentation-only. They are derived deterministically from the
 * observation id, so a given memory keeps its place between sessions — a point
 * that wandered every reload would be worse than no globe at all.
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var R = 112;                 // sphere radius, per the design spec
  var CX = 150, CY = 150;      // centre of the 300×300 viewBox
  var DRAG_SLOP = 3;           // px of movement before a tap becomes a drag

  var TYPE_COLOR = {
    location: '#4A90E2',
    item: '#34A853',
    activity: '#BD10E0'
  };

  /* Stable pseudo-random placement from a string id. */
  function hash(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0) / 4294967295;
  }

  function placement(observation) {
    var a = hash(observation.id || observation.label || 'x');
    var b = hash('lon:' + (observation.id || observation.label || 'x'));
    return {
      // Bias away from the poles so points stay legible and evenly spread.
      lat: (a * 130) - 65,
      lon: b * 360
    };
  }

  function Globe(svgEl, tooltipEl, opts) {
    opts = opts || {};
    this.svg = svgEl;
    this.tooltip = tooltipEl;
    this.onSelect = opts.onSelect || function () {};
    this.observations = [];
    this.rot = 0;
    this.selected = null;
    this.highlighted = null;
    this.filter = 'all';
    this._dragging = false;
    this._moved = 0;
    this._lastX = 0;
    this._bindDrag();
  }

  Globe.prototype._bindDrag = function () {
    var self = this;
    this.svg.addEventListener('pointerdown', function (e) {
      self._dragging = true;
      self._moved = 0;
      self._lastX = e.clientX;
      self.svg.setPointerCapture(e.pointerId);
    });
    this.svg.addEventListener('pointermove', function (e) {
      if (!self._dragging) { return; }
      var dx = e.clientX - self._lastX;
      self._lastX = e.clientX;
      self._moved += Math.abs(dx);
      self.rot += dx * 0.45;          // horizontal axis only — no tumbling
      self.render();
    });
    function release(e) {
      if (!self._dragging) { return; }
      self._dragging = false;
      try { self.svg.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
    }
    this.svg.addEventListener('pointerup', release);
    this.svg.addEventListener('pointercancel', release);
  };

  Globe.prototype.setObservations = function (observations) {
    this.observations = (observations || []).map(function (o) {
      var place = o.lat === undefined ? placement(o) : { lat: o.lat, lon: o.lon };
      return Object.assign({}, o, place);
    });
    if (this.selected && !this.observations.some(function (o) { return o.id === this.selected; }, this)) {
      this.selected = null;
    }
    this.render();
  };

  Globe.prototype.setFilter = function (filter) {
    this.filter = filter;
    if (this.selected) {
      var still = this.visible().some(function (o) { return o.id === this.selected; }, this);
      if (!still) { this.selected = null; }
    }
    this.render();
  };

  Globe.prototype.highlight = function (id) {
    this.highlighted = id;
    this.render();
  };

  Globe.prototype.select = function (id) {
    this.selected = (this.selected === id) ? null : id;
    this.render();
    this.onSelect(this.selected);
  };

  Globe.prototype.visible = function () {
    var filter = this.filter;
    return this.observations.filter(function (o) {
      return filter === 'all' || o.type === filter;
    });
  };

  /* Project (lat, lon) onto the sphere at the current rotation. */
  Globe.prototype._project = function (o) {
    var lat = o.lat * Math.PI / 180;
    var lon = (o.lon + this.rot) * Math.PI / 180;
    var x = Math.cos(lat) * Math.sin(lon);
    var y = Math.sin(lat);
    var z = Math.cos(lat) * Math.cos(lon);   // >0 = facing the viewer
    return { cx: CX + x * R, cy: CY - y * R, z: z };
  };

  Globe.prototype.render = function () {
    var self = this;
    // Wipe only the points layer; the sphere and its rings are static markup.
    var layer = this.svg.querySelector('#globePoints');
    while (layer.firstChild) { layer.removeChild(layer.firstChild); }

    var points = this.visible().map(function (o) {
      return Object.assign({}, o, self._project(o));
    });
    // Painter's algorithm: far side first, so near points sit on top.
    points.sort(function (a, b) { return a.z - b.z; });

    points.forEach(function (p) {
      var isSelected = p.id === self.selected;

      if (p.id === self.highlighted) {
        var ring = document.createElementNS(NS, 'circle');
        ring.setAttribute('cx', p.cx);
        ring.setAttribute('cy', p.cy);
        ring.setAttribute('r', (5 + 2.4 * p.z) + 8);
        ring.setAttribute('fill', 'none');
        ring.setAttribute('stroke', TYPE_COLOR[p.type] || '#4A90E2');
        ring.setAttribute('stroke-width', '2');
        ring.setAttribute('opacity', '0.35');
        ring.setAttribute('class', 'globe-ring');
        layer.appendChild(ring);
      }

      var dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', p.cx);
      dot.setAttribute('cy', p.cy);
      dot.setAttribute('r', (5 + 2.4 * p.z) + (isSelected ? 2 : 0));
      dot.setAttribute('fill', TYPE_COLOR[p.type] || '#4A90E2');
      // Far points recede rather than disappearing, so the sphere reads as full.
      dot.setAttribute('opacity', 0.4 + 0.6 * ((p.z + 1) / 2));
      dot.setAttribute('class', 'globe-dot');
      dot.setAttribute('tabindex', '0');
      dot.setAttribute('role', 'button');
      dot.setAttribute('aria-label', p.label + ' ' + (p.place || '') + ' ' + (p.time || ''));
      dot.addEventListener('click', function () {
        if (self._moved > DRAG_SLOP) { return; }   // that was a drag, not a tap
        self.select(p.id);
      });
      dot.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); self.select(p.id); }
      });
      layer.appendChild(dot);
    });

    this._renderTooltip(points);
  };

  Globe.prototype._renderTooltip = function (points) {
    var selected = null;
    for (var i = 0; i < points.length; i++) {
      if (points[i].id === this.selected) { selected = points[i]; break; }
    }
    if (!selected) {
      this.tooltip.style.opacity = '0';
      this.tooltip.setAttribute('aria-hidden', 'true');
      return;
    }
    var bits = [selected.label, selected.place, selected.time].filter(Boolean);
    this.tooltip.textContent = bits.join(' • ');
    // Clamped so the bubble can never spill off a 375px screen.
    this.tooltip.style.left = Math.min(188, Math.max(112, selected.cx)) + 'px';
    this.tooltip.style.top = Math.max(44, selected.cy) + 'px';
    this.tooltip.style.opacity = '1';
    this.tooltip.setAttribute('aria-hidden', 'false');
  };

  Globe.TYPE_COLOR = TYPE_COLOR;
  global.Globe = Globe;
})(window);
