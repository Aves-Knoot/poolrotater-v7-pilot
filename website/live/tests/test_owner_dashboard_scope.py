"""Execute the live dashboard and chart-label bridge in distinct JS scopes."""
from pathlib import Path
import json
import unittest
import quickjs

SOURCE=(Path(__file__).resolve().parents[3]/'website/live/template/script.js').read_text()
OWNER=SOURCE.index('/* Lightweight current-room updates;')

def block(start):
    opening=SOURCE.index('{',start);depth=0
    for index in range(opening,len(SOURCE)):
        if SOURCE[index]=='{':depth+=1
        elif SOURCE[index]=='}':
            depth-=1
            if depth==0:return SOURCE[start:index+1]
    raise AssertionError('unclosed JavaScript block')

class OwnerDashboardScope(unittest.TestCase):
    def test_dashboard_updates_without_access_to_chart_local_variables(self):
        ctx=quickjs.Context()
        ctx.eval('''
          const live={querySelector:s=>s==='.pr-dashboard-health'?{dataset:{},querySelector:()=>null}:null,querySelectorAll:()=>[]};
          const document={querySelector:()=>live,querySelectorAll:()=>[]};
          const window={prLiveOccupancyUpdate:()=>{},prLiveOwnerRoomInfo:(r,s)=>({history:'Lobby '+s})};
          function now(){return 2000;}
          function integer(x){return Number.isSafeInteger(x)&&x>0;}
        ''')
        ctx.eval(block(SOURCE.index('function text(',OWNER)))
        ctx.eval(block(SOURCE.index('function clock(',OWNER)))
        ctx.eval(block(SOURCE.index('function dashboard(',OWNER)))
        for valid in (False,True):
            with self.subTest(valid=valid):
                data={'rooms':[{'alias':'room1','room_id':101,'players':2,'state':'IDLE'}], 'service':{'online':True}}
                ctx.eval('dashboard('+json.dumps(data)+','+str(valid).lower()+')')
        # Both names belong to the other initializer, never global dashboard dependencies.
        self.assertEqual(ctx.eval('typeof labels'), 'undefined')
        self.assertEqual(ctx.eval('typeof graphs'), 'undefined')

    def test_status_event_updates_chart_labels_in_the_chart_scope_only(self):
        marker=SOURCE.index("document.addEventListener('pr:lobby-status',")
        handler=block(SOURCE.index('function(event)',marker))
        ctx=quickjs.Context()
        ctx.eval('''
          let labels=['Lobby 1','Lobby 2','Lobby 3','Lobby 4'],graphCalls=0;
          const live={};const window={prLiveOwnerRoomInfo:(r,s)=>({history:'Lobby '+s+(r?' '+r.type:'')})};
          function graphs(){graphCalls++;}
        ''')
        ctx.eval('const handler='+handler+';')
        ctx.eval("handler({detail:{rooms:[{alias:'room3',type:'Taiko'}]}})")
        self.assertEqual(ctx.eval('labels[2]'),'Lobby 3 Taiko')
        self.assertEqual(ctx.eval('graphCalls'),1)
        ctx.eval("handler({detail:{rooms:[{alias:'room3',type:'Taiko'}]}});handler({detail:null})")
        self.assertEqual(ctx.eval('graphCalls'),1)
        ctx.eval("handler({detail:{rooms:[{alias:'room3',type:'Catch'}]}})")
        self.assertEqual(ctx.eval('labels[2]'),'Lobby 3 Catch')
        self.assertEqual(ctx.eval('graphCalls'),2)

if __name__=='__main__':unittest.main()
