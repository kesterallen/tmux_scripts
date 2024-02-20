
"""
Take the output from 
    tmux list-p -aF "#S;#I;#P;#W;#{pane_current_path};#{pane_pid}" 

e.g.:
    scorpius;1;1;gcloud;/home/kester/Dropbox/gcloud/projects/sf_p3;4190
    scorpius;1;2;gcloud;/home/kester/Dropbox/export/apple_health_export;4197
    scorpius;2;1;analytics and strainbanking and staging db;/home/kester;4203
    scorpius;2;2;analytics and strainbanking and staging db;/home/kester;4209
    scorpius;2;3;analytics and strainbanking and staging db;/home/kester;4221
    scorpius;2;4;analytics and strainbanking and staging db;/home/kester;169213
    scorpius;3;1;meetings;/home/kester;4231
    scorpius;4;1;codility;/home/kester/codility;4237
    scorpius;5;1;discord_music;/home/kester/Desktop;4248
    scorpius;5;2;discord_music;/home/kester/src/ayb-bot;4265
    scorpius;5;3;discord_music;/home/kester/src;23582
    scorpius;6;1;footpedal;/home/kester;4276
    scorpius;7;1;hermes robot protocol docs;/home/kester/src/hermes;4294
    scorpius;8;1;bash;/home/kester;299586

and modify that to include the command that is generating the process ID (pane_pid) argument as a new final column:

    scorpius;1;1;gcloud;/home/kester/Dropbox/gcloud/projects/sf_p3;4190;vim surlyfritter/scripts/wordle.py
    scorpius;1;2;gcloud;/home/kester/Dropbox/export/apple_health_export;4197;
    scorpius;2;1;analytics and strainbanking and staging db;/home/kester;4203;
    scorpius;2;2;analytics and strainbanking and staging db;/home/kester;4209;
    scorpius;2;3;analytics and strainbanking and staging db;/home/kester;4221;/home/kester/.virtualenv/twitter/bin/python /home/kester/.virtualenv/twitter/bin/pgcli -hdbtl.stage.amyris.local -p9999 -Upgdb -dpgdbstage
    scorpius;2;4;analytics and strainbanking and staging db;/home/kester;169213;
    scorpius;3;1;meetings;/home/kester;4231;vim notes/meeting_notes.txt
    scorpius;4;1;codility;/home/kester/codility;4237;
    scorpius;5;1;discord_music;/home/kester/Desktop;4248;
    scorpius;5;2;discord_music;/home/kester/src/ayb-bot;4265;vim ayb-bot/bot.py
    scorpius;5;3;discord_music;/home/kester/src;23582;
    scorpius;6;1;footpedal;/home/kester;4276;vim /home/kester/src/javascript_timing.html
    scorpius;7;1;hermes robot protocol docs;/home/kester/src/hermes;4294;
    scorpius;8;1;bash;/home/kester;299586;


Originally generated by this cronjob:

    # tmux list-s && (tmux list-p -aF "#S;#I;#P;#W;#{pane_current_path};#{pane_pid}" | tee /tmp/.tmux1 | cut -d\; -f6 | xargs -I{} -n1 bash -c 'ps -hoargs --ppid {} || echo' >| /tmp/.tmux2; paste -d\; /tmp/.tmux? >| ~/tmux_state.txt)

But now by this script:

    tmux list-s && (tmux list-p -aF "#S;#I;#P;#W;#{pane_current_path};#{pane_pid}" | python3 ~/src/tmux_scripts/tmux_state_record.py  >| ~/tmux_state.txt)

"""

import subprocess
import sys

for line in sys.stdin:
    line = line.rstrip()
    ppid = line.split(";")[-1]
    command = ["ps", "-hoargs", "--ppid", ppid]
    # Grab only first PID output if there are multiple, e.g. one or more suspended jobs in the pane
    result = subprocess.run(command, capture_output=True).stdout.decode("utf-8").rstrip().split("\n")[0]
    print(";".join([line, result]))

