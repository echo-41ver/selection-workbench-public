"""Synthetic structured facts; these do not measure model/image accuracy."""
def identity(verdict="SAME"):
    return {"verdict": verdict,
            "design_comparison": {'relation':'SAME','amazon':'左侧圆弧连接矩形主体，右下角两条平行凹槽',
                                  'temu':'左侧圆弧连接矩形主体，右下角两条平行凹槽'},
            "matches": [{"feature": "主体轮廓", "amazon": "左侧圆弧连接矩形主体", "temu": "左侧圆弧连接矩形主体"},
                        {"feature": "边缘结构", "amazon": "右下角两条平行凹槽", "temu": "右下角两条平行凹槽"}],
            "differences": [], "uncertainties": [],
            "reason": "圆弧主体连接和右下角双凹槽对应位置一致"}
