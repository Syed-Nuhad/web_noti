from webnotify.models import NotificationSource
import os, json

sid = 33
s = NotificationSource.objects.get(pk=sid)
ex = s.extra_config or {}
ex.update({'debug':True,'rendered':True,'render_timeout_ms':45000,'user_data_dir':os.path.abspath('desktop_client/.profiles')})
for k in ('last_count','last_hash','fingerprint','mode'):
    ex.pop(k, None)
s.extra_config = ex
s.save(update_fields=['extra_config'])
print('Updated', s.id)
print(json.dumps(s.extra_config, indent=2))
