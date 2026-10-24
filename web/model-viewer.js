import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

export async function mountModel(card, url) {
    const canvas = card.querySelector('.model-canvas');
    const stage = card.querySelector('.model-stage');
    const empty = card.querySelector('[data-model-empty]');
    if (typeof url !== 'string' || !url.startsWith('/api/model_stl?')) {
        throw new Error('неизвестный источник STL');
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);
    let response, buffer;
    try {
        response = await fetch(url, {signal: controller.signal});
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'HTTP ' + response.status);
        }
        buffer = await response.arrayBuffer();
    } finally {
        clearTimeout(timer);
    }
    if (!card.isConnected) return null;
    if (buffer.byteLength > 50 * 1024 * 1024) throw new Error('модель больше 50 МБ');
    const geometry = new STLLoader().parse(buffer);
    const position = geometry.getAttribute('position');
    if (!position || !position.count || position.count > 1500000 ||
        !position.array.every(Number.isFinite)) {
        geometry.dispose();
        throw new Error('некорректная сетка или более 500000 треугольников');
    }
    // Scene export is already Y-up. Original STL files conventionally use Z-up.
    if (response.headers.get('X-Model-Up-Axis') !== 'Y') geometry.rotateX(-Math.PI / 2);
    geometry.computeBoundingBox();
    const size = geometry.boundingBox.getSize(new THREE.Vector3());
    geometry.center();
    geometry.computeVertexNormals();
    const radius = Math.max(size.length() / 2, 0.01);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#f8f6f2');
    const material = new THREE.MeshStandardMaterial({color: '#df7900', roughness: 0.64, metalness: 0.05, side: THREE.DoubleSide});
    scene.add(new THREE.Mesh(geometry, material));
    scene.add(new THREE.HemisphereLight(0xffffff, 0x746957, 2));
    const light = new THREE.DirectionalLight(0xffffff, 3);
    light.position.set(radius, radius * 2, radius * 3);
    scene.add(light);
    let renderer;
    try {
        renderer = new THREE.WebGLRenderer({canvas: canvas, antialias: true});
    } catch (error) {
        geometry.dispose(); material.dispose(); throw error;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const camera = new THREE.PerspectiveCamera(40, 1, radius / 1000, radius * 1000);
    const controls = new OrbitControls(camera, canvas);
    controls.minDistance = radius * 0.1;
    controls.maxDistance = radius * 100;
    let disposed = false;
    const draw = () => { if (!disposed) renderer.render(scene, camera); };
    const resize = () => {
        if (disposed) return;
        const width = Math.max(stage.clientWidth, 1), height = Math.max(stage.clientHeight, 240);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        draw();
    };
    stage.classList.add('has-model');
    resize();
    const fov = Math.min(THREE.MathUtils.degToRad(camera.fov),
        2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * camera.aspect));
    const distance = radius / Math.sin(fov / 2) * 1.15;
    camera.position.copy(new THREE.Vector3(1, 0.7, 1).normalize().multiplyScalar(distance));
    controls.update();
    controls.saveState();
    controls.addEventListener('change', draw);
    const reset = () => controls.reset();
    canvas.addEventListener('dblclick', reset);
    const observer = new ResizeObserver(resize);
    observer.observe(stage);
    empty.hidden = true;
    const label = card.querySelector('[data-model-size]');
    if (label) label.textContent = 'X × высота × Z: ' + [size.x, size.y, size.z].map(v => v.toFixed(2)).join(' × ') + ' мм';
    draw();
    return {dispose: function () {
        if (disposed) return;
        disposed = true;
        observer.disconnect(); controls.dispose(); canvas.removeEventListener('dblclick', reset);
        geometry.dispose(); material.dispose(); renderer.dispose(); renderer.forceContextLoss();
        if (card.isConnected) {
            stage.classList.remove('has-model');
            empty.hidden = false;
            empty.textContent = 'Превью освобождено для новых карточек.';
        }
    }};
}
