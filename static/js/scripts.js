let accessToken = '';

// Check if logged in
if (!localStorage.getItem('email')) {
    emailModal.show();
} else {
    document.getElementById('user-email').textContent = localStorage.getItem('email');
}

async function submitEmail() {
    const email = document.getElementById('email-input').value;
    const password = prompt('Enter password'); // Simple for testing
    if (email.includes('@') && email.includes('.') && password) {
        const response = await fetch('/api/login/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        if (response.ok) {
            const data = await response.json();
            accessToken = data.access;
            localStorage.setItem('email', email);
            document.getElementById('user-email').textContent = email;
            emailModal.hide();
        } else {
            alert('Invalid credentials');
        }
    } else {
        alert('Invalid email or password');
    }
}

// Add Authorization header to API calls
async function addUrl() {
    const url = document.getElementById('url-input').value;
    const css_selector = document.getElementById('css-selector').value;
    if (url.startsWith('http')) {
        const response = await fetch('/api/urls/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({ url, css_selector })
        });
        if (response.ok) {
            updateUrlList();
            document.getElementById('url-input').value = '';
            document.getElementById('css-selector').value = '';
        } else {
            alert('Error adding URL');
        }
    } else {
        alert('Invalid URL');
    }
}

async function updateUrlList() {
    const response = await fetch('/api/urls/', {
        headers: { 'Authorization': `Bearer ${accessToken}` }
    });
    const urls = await response.json();
    const urlList = document.getElementById('url-list');
    urlList.innerHTML = '';
    urls.forEach(item => {
        const li = document.createElement('li');
        li.className = 'list-group-item d-flex justify-content-between';
        li.innerHTML = `${item.url} ${item.css_selector ? '(' + item.css_selector + ')' : ''} <button class="btn btn-sm btn-danger" onclick="removeUrl('${item.url}')">Remove</button>`;
        urlList.appendChild(li);
    });
}

async function removeUrl(url) {
    await fetch(`/api/urls/${url}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${accessToken}` }
    });
    updateUrlList();
}



document.getElementById('sound-input').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    if (file && ['audio/mpeg', 'audio/mp4', 'audio/wav'].includes(file.type)) {
        const formData = new FormData();
        formData.append('sound', file);
        const response = await fetch('/api/sound/', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${accessToken}` },
            body: formData
        });
        if (!response.ok) {
            alert('Error uploading sound');
        }
    } else {
        alert('Unsupported file format');
    }
});

document.getElementById('ring-count').addEventListener('change', async (event) => {
    const count = parseInt(event.target.value);
    if (count >= 1 && count <= 5) {
        await fetch('/api/settings/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({ ring_count: count })
        });
    }
});

async function startMonitoring() {
    await fetch('/api/start_monitoring/', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${accessToken}` }
    });
    document.getElementById('status').textContent = 'Monitoring';
    pollNotifications();
}

async function pollNotifications() {
    setInterval(async () => {
        const response = await fetch('/api/notifications/', {
            headers: { 'Authorization': `Bearer ${accessToken}` }
        });
        const notifications = await response.json();
        notifications.forEach(async (msg) => {
            const modal = document.getElementById('notificationModal');
            document.getElementById('notification-message').textContent = msg;
            modal.style.display = 'flex';
            const soundResponse = await fetch('/api/sound/', {
                headers: { 'Authorization': `Bearer ${accessToken}` }
            });
            const soundBlob = await soundResponse.blob();
            const soundUrl = URL.createObjectURL(soundBlob);
            const audio = new Audio(soundUrl);
            let count = parseInt(document.getElementById('ring-count').value);
            let played = 0;
            function play() {
                if (played < count) {
                    audio.play();
                    played++;
                    audio.onended = play;
                }
            }
            play();
        });
    }, 10000);
}



async function loadSettings() {
  try {
    const data = await fetchJSON('/api/settings/');
    const url = data?.settings?.default_ringtone_url;
    if (url) {
      const src = document.getElementById('alarmSrc');
      src.src = url;
      document.getElementById('alarmAudio').load();
    }
  } catch {}
}


    document.addEventListener('DOMContentLoaded', function(){
      var y = document.getElementById('year'); if (y) y.textContent = new Date().getFullYear();
    });