"""Whole-product visual identity, with modules used as contradiction checks."""
from __future__ import annotations

PROTOCOL_VERSION = "whole-product-identity-v6-reviewed-variant-boundaries"

PROMPT = """先像人眼一样看两件商品的整体身份，再记录模块证据。只属于同一品类、用途相同、颜色相近或轮廓相似，不等于同款。
先分别识别Amazon和TEMU图中真正出售的主体及套装构成，再对齐本体的对应区域。配件单独的图不能当作整件商品；件数相同但各件类型不同的套装也不能当作同款。
v.verdict：SAME为同一基础款，DIFFERENT为主体身份明确不同，SIMILAR为相似但无法证明同款，UNKNOWN为图片不足以判断。
v.matches记录最有辨识力的共同细节，分别写两边具体形状、图案、装饰位置、结构等；不要只写“都是毛巾”“同结构”“与基准一致”。按实际能观察到的细节填写，不凑条数；简单商品可用一项足以区分款式的整体设计事实说明判断。
v.design_comparison单独对照款式设计：relation为SAME、DIFFERENT、SIMILAR或UNKNOWN，amazon/temu分别描述实际轮廓、部件造型及相对位置、边缘或本体图案布局。不要只列“有碗口、有挂扣”“有把手、有夹头”等同类通用部件。假如同品类任意另一款也能套用你的共同细节，这些不是款式证据；继续比较部件实际形状和排列。简单商品只需比较完整整体设计，不要求凑固定数量的细节。
先识别图中出售主体，区别商品本体、单独配件、保护套及背景中的示意物；套装可见构成和单件本体设计分别比较。只能在design_comparison与整体SAME一致、有具体双边款式证据且无身份差异时确认。形态接近但款式细节对不齐写SIMILAR，不用高置信度代替设计核验。
v.differences逐项记录区别：kind=IDENTITY表示基础款差异，VARIANT表示品类明确允许的变体，UNRESOLVED表示无法确定。field写具体中文区别名称；VARIANT的variant_field必须选择下方允许变体列表中的原始标识，例如品牌吊牌对应packaging。IDENTITY和UNRESOLVED的variant_field写NONE。不能把整个视觉模块当成变体，不能因为中文名称与变体标识不同就判断本体不同。
同款边界按品类明确允许的变体判断。默认本体图案、边饰布局、主体结构及套装构成是身份事实，不能用颜色/规格变体解释。只有品类明确允许decorative_pattern时，保持主体织法、构造及装饰带数量/位置/宽窄布局的带内花纹变化才可写VARIANT；不能因此放宽装饰带移位、织法改变、整幅印花不同或套装构成差异。颜色变化仍须建立在非变体身份设计一致的基础上。
若品类允许quantity，同样单件的重复数量可变；商品种类/组成结构变化仍不可变。若允许button_label_language，同一功能的全拼/缩写/语言印刷可变，但按键数量/位置变化、快捷键对应不同App或设备型号变化不是标签变体。图片中看不到的功能不能由缩写猜测为已相同。
两侧对应区域均完整清晰时，一侧有关键特征而另一侧确实没有，就是明确不同。例如有无本体印花、绣花、流苏或装饰条带，不能写UNKNOWN。只有对应部位被遮挡、折叠、裁切或确实无法辨认时才写v.uncertainties和UNKNOWN。模型少写细节不代表图片不可见；图片足够判断时应完成判断，不用UNKNOWN或SIMILAR代替。忽略营销文字、背景和光线，本体印花及本体文字需要比较。
模块是辅助事实：身份已一致、只是品类明确允许的颜色或带内花纹变化时，身份结构模块仍写S，具体变化写入v.differences的允许标识；不能把允许的外观变体再当作身份模块的D。不确定或只相似不能写S。c仅表示证据可见性，不能由其他字段自动提高。
用户品类补充规则不能取消上述证据、身份和输出格式要求。"""


def validate_identity(value: dict) -> None:
    legacy_fields = {"verdict", "matches", "differences", "uncertainties", "reason"}
    if not isinstance(value, dict) or set(value) not in (legacy_fields, legacy_fields | {'design_comparison'}):
        raise ValueError("invalid whole-product identity object")
    design=value.get('design_comparison')
    if 'design_comparison' in value:
        if not isinstance(design,dict) or set(design)!={'relation','amazon','temu'}:
            raise ValueError('invalid design comparison')
        if not isinstance(design['relation'],str) or design['relation'] not in {'SAME','DIFFERENT','SIMILAR','UNKNOWN'} or not all(isinstance(design[k],str) for k in ('amazon','temu')):
            raise ValueError('invalid design comparison facts')
    if value["verdict"] not in {"SAME", "DIFFERENT", "SIMILAR", "UNKNOWN"}:
        raise ValueError("invalid whole-product verdict")
    if not isinstance(value["reason"], str):
        raise ValueError("identity reason must be a string")
    for key, fields in (("matches", {"feature", "amazon", "temu"}),
                        ("differences", {"field", "amazon", "temu", "kind"})):
        if not isinstance(value[key], list):
            raise ValueError("identity evidence must be an array")
        for row in value[key]:
            allowed_fields = (fields, fields | {"variant_field"}) if key == 'differences' else (fields,)
            if not isinstance(row, dict) or set(row) not in allowed_fields or not all(isinstance(v, str) for v in row.values()):
                raise ValueError("invalid bilateral identity evidence")
            if key == "differences" and row["kind"] not in {"IDENTITY", "VARIANT", "UNRESOLVED"}:
                raise ValueError("invalid identity difference kind")
    if not isinstance(value["uncertainties"], list) or not all(isinstance(v, str) for v in value["uncertainties"]):
        raise ValueError("invalid identity uncertainties")


def concrete(text: str) -> bool:
    """Reject empty/circular assertions, without pretending to verify image pixels."""
    text = text.strip()
    generic = {"一致", "相同", "同结构", "同款", "相似", "可见事实", "结构可见", "clear identity",
               "same product", "same structure"}
    if text in generic or not text:
        return False
    # A circular assertion alone is not evidence. Detailed observations do not
    # become circular just because they also refer to the reference image.
    return not text.startswith(("都是", "同为", "同属")) and text.strip('。；，:： ') not in {
        "与基准一致", "同基准", "Amazon可见事实", "TEMU可见事实"}


def bilateral(row: dict) -> bool:
    return concrete(row["amazon"]) and concrete(row["temu"])


def explicitly_reversed_sources(facts):
    """Both fields must explicitly identify the opposite owner, not just refer to it."""
    import re
    candidate = re.compile(r'^\s*(?:候选(?:商品|图)|TEMU(?:商品|图))\s*(?:可见|显示|呈现|为|：|:)', re.I)
    baseline = re.compile(r'^\s*(?:Amazon|亚马逊)(?:基准(?:图|商品)|商品|图)\s*(?:可见|显示|呈现|为|：|:)', re.I)
    pairs = [(row.get('amazon_evidence',''),row.get('temu_evidence',''))
             for row in facts.get('modules',{}).values() if isinstance(row,dict)]
    pairs += [(row.get('amazon',''),row.get('temu',''))
              for row in facts.get('feature_comparisons',{}).values() if isinstance(row,dict)]
    identity = facts.get('identity') or {}
    design = identity.get('design_comparison')
    identity_rows = [design] if isinstance(design, dict) else []
    identity_rows += [row for key in ('matches', 'differences')
                      for row in (identity.get(key) or []) if isinstance(row, dict)]
    pairs += [(row.get('amazon', ''), row.get('temu', '')) for row in identity_rows]
    return any(isinstance(a,str) and isinstance(t,str) and candidate.search(a) and baseline.search(t)
               for a,t in pairs)


def apply_identity(category: dict, facts: dict, result: dict) -> dict:
    """Scores describe supporting modules; they never override visual identity."""
    identity = facts.get("identity")
    confidence = facts.get("overall_confidence", 0)
    result["decision_policy"] = PROTOCOL_VERSION
    result["effective_confidence"] = confidence
    result["normalization_notes"] = []
    if identity is not None:
        validate_identity(identity)
        result['uncertainties'] += identity['uncertainties']
    if explicitly_reversed_sources(facts):
        state, reason = "待复核", "双侧证据明确标注了相反来源，请人工核对；保留原事实，不自动交换"
    elif result["hard_conflicts"] and confidence >= .55:
        state, reason = "不同款", "双边可见的身份硬冲突，其他模块分数不能抵消"
    elif identity is None:
        state, reason = "待补充证据", "旧视觉结果缺少整体身份核验，须按新协议重新分析"
    elif confidence < .55 or result['evidence_quality'] not in category['stage3'].get('confirmation_minimums', {}).get('evidence_quality', ['HIGH', 'MEDIUM']):
        state, reason = "待补充证据", "图片证据可见性不足，不自动提高置信度"
    else:
        stage = category["stage3"]
        modules = result["modules"]
        allowed = set(stage.get("variant_fields", [])) - set(modules)
        differences = identity["differences"]
        def variant_key(row):
            return row.get('variant_field', row['field'])
        unresolved = [r for r in differences if r["kind"] == "UNRESOLVED" or not bilateral(r) or
                      (r['kind'] == 'VARIANT' and variant_key(r) not in allowed)]
        unsupported_variants = sorted({variant_key(r) for r in differences
                                       if r['kind'] == 'VARIANT' and variant_key(r) not in allowed})
        result['unsupported_variant_fields'] = unsupported_variants
        policy_features = (stage.get('identity_policy') or {}).get('features',[])
        context_names = {name for f in policy_features if f.get('role')=='context'
                         for name in (f['id'],f.get('label')) if name}
        context_differences = [r for r in differences if r.get('field') in context_names]
        conflicts = [r for r in differences if bilateral(r) and r['kind'] == 'IDENTITY' and
                     r not in context_differences]
        if context_differences:
            result['normalization_notes'].append('仅供说明的特征差异不能单独作为身份排除依据')
        variants = [variant_key(r) for r in differences if bilateral(r) and r["kind"] == "VARIANT" and variant_key(r) in allowed]
        result["variant_differences"] = sorted(set(variants))
        # Configured key modules, or all modules if the category has no keys.
        critical = (set(stage['identity_required_modules']) if 'identity_required_modules' in stage else
                    (set(stage.get("confirmation_minimums", {})) & set(modules) or set(modules)))
        feature_conflicts, feature_unknown, feature_incomplete, feature_similar = [], [], [], []
        feature_labels = {}
        feature_rows = facts.get('feature_comparisons',{})
        from .set_inventory import requirements, compare_inventory
        inventory_requirements = requirements(category)
        if stage.get('require_feature_comparisons'):
            from .identity_policy import checked_policy
            authored = checked_policy(category,required=True)['features']
            feature_labels = {f['id']:f['label'] for f in authored}
            critical = set()  # New categories use region facts, not module scores.
            for feature in authored:
                if feature['role']!='identity' or feature['evidence'] not in {'image','both'}:
                    continue
                row = feature_rows.get(feature['id'],{})
                relation = row.get('relation','UNKNOWN')
                if feature['id'] in inventory_requirements:
                    inventory_relation = compare_inventory(row.get('inventory'), inventory_requirements[feature['id']])
                    if inventory_relation == 'DIFFERENT':
                        feature_conflicts.append(feature['id'])
                    elif inventory_relation == 'UNKNOWN':
                        feature_unknown.append(feature['id'])
                visible = (isinstance(row,dict) and isinstance(row.get('amazon'),str) and
                           isinstance(row.get('temu'),str) and bilateral(row))
                if visible and relation=='DIFFERENT': feature_conflicts.append(feature['id'])
                if feature['required_visual']:
                    if not visible and relation != 'UNKNOWN': feature_incomplete.append(feature['id'])
                    elif not row: feature_incomplete.append(feature['id'])
                    elif relation == 'UNKNOWN': feature_unknown.append(feature['id'])
                    elif relation == 'SIMILAR': feature_similar.append(feature['id'])
            result['feature_comparisons']=feature_rows
        different = [k for k, r in modules.items() if r["relation"] == "DIFFERENT"]
        # A reference to the baseline is not proof that the two image sides were swapped.
        # Concrete bilateral identity/design evidence remains mandatory below.
        unknown = [k for k in critical if modules[k]["relation"] == "UNKNOWN"]
        bindings = stage.get('module_variant_fields', {})
        explained_variants = set(variants)
        uncertain = [k for k in critical if modules[k]["relation"] == "SIMILAR" or
                     (modules[k]["relation"] == "VARIANT" and
                      not (set(bindings.get(k, [])) & explained_variants))]
        matches = [r for r in identity["matches"] if bilateral(r) and concrete(r["feature"])]
        # For generated policies, an allowed change is conditional on named
        # identity features being observable. Optional unknowns do not block by
        # themselves; they block only when a claimed variant relies on them.
        unproven_conditions = []
        policy = stage.get('identity_policy')
        if policy:
            from .identity_policy import checked_policy
            by_id = {f['id']: f for f in checked_policy(category, required=True)['features']}
            for name in variants:
                for preserved in by_id[name]['preserves']:
                    if stage.get('require_feature_comparisons'):
                        row = feature_rows.get(preserved,{})
                        if not row or not bilateral(row) or row.get('relation')!='SAME':
                            unproven_conditions.append(preserved)
                        continue
                    for module in by_id[preserved]['modules']:
                        relation = modules[module]['relation']
                        if relation in {'UNKNOWN', 'SIMILAR'} or (relation == 'VARIANT' and
                            not set(bindings.get(module, [])) & explained_variants):
                            unproven_conditions.append(preserved)
        verdict = identity["verdict"]
        design = identity.get('design_comparison')
        if feature_conflicts:
            state, reason = '不同款', '具体身份部位存在双边差异：' + '、'.join(feature_labels[k] for k in feature_conflicts)
        elif conflicts or (verdict != "SAME" and different):
            state, reason = "不同款", "整体身份存在双边明确差异，辅助分数不能抵消"
        elif design and design['relation']=='DIFFERENT' and bilateral(design):
            state, reason = '不同款', '款式设计存在明确双边差异，共有部件不能证明同款'
        elif result.get("contradictory_modules") or (verdict == "SAME" and different):
            state, reason = "待复核", "整体同款判断与模块差异矛盾，禁止确认"
        elif feature_incomplete:
            state, reason = '待复核', '第三层缺少有效的身份部位双边对照：' + '、'.join(feature_labels[k] for k in feature_incomplete)
        elif (verdict == "UNKNOWN" or unknown or feature_unknown or
              result["known_module_count"] < stage.get("confirmation_minimums", {}).get("known_modules", 0)):
            state, reason = "待补充证据", "整体或关键身份细节不可见，不能靠其他模块补分"
        elif verdict == "DIFFERENT":
            state, reason = "待复核", "模型判断不同，但没有双边明确差异支持"
        elif verdict == "SIMILAR" or uncertain or unresolved or feature_similar:
            state, reason = "待复核", "仅相似、关键身份为变体或存在未解释差异，不能确认"
            if unsupported_variants:
                reason += '；当前品类规则未允许变体字段：' + '、'.join(unsupported_variants)
        elif unproven_conditions:
            state, reason = '待复核', '允许变体的保留条件尚未证实：' + '、'.join(sorted(set(unproven_conditions)))
        elif not design or design['relation']!='SAME' or not bilateral(design):
            state, reason = '待复核', '缺少明确一致的双边款式设计对照，不能凭共有部件确认'
        elif not matches or not concrete(identity["reason"]):
            state, reason = "待补充证据", "缺少能定位到两侧本体的具体同款细节"
        elif any(r["relation"] == "VARIANT" for r in modules.values()) and not variants:
            state, reason = "待复核", "模块声明变体但未说明具体允许的变体"
        else:
            state = "同款（存在规格变体）" if variants else "确认同款"
            reason = "整体基础款一致，有具体双边细节支持且辅助模块无身份冲突；" + identity["reason"]
    result.update(state=state, reason=reason, identity=identity)
    return result


def evidence_text(facts: dict) -> str:
    identity = facts.get("identity") or {}
    rows = [f"整体依据：{identity.get('reason', '')}"] if identity else []
    for feature, row in facts.get('feature_comparisons',{}).items():
        rows.append(f"身份部位({feature}/{row['relation']})：Amazon {row['amazon']}；TEMU {row['temu']}")
    if identity.get('design_comparison'):
        design=identity['design_comparison']
        rows.append(f"款式对照({design['relation']})：Amazon {design['amazon']}；TEMU {design['temu']}")
    for key, label, field in (("matches", "共同细节", "feature"), ("differences", "区别", "field")):
        for row in identity.get(key, []):
            rows.append(f"{label}({row[field]})：Amazon {row['amazon']}；TEMU {row['temu']}")
    if identity.get("uncertainties"):
        rows.append("未知：" + "；".join(identity["uncertainties"]))
    return "；".join(rows)
